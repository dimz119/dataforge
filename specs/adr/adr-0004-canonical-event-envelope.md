# ADR-0004 — Canonical event envelope, frozen first, additive-only thereafter

**Deliverable:** D17

Every DataForge event — business and CDC, canonical and chaos-transformed, on every channel — carries one canonical envelope, frozen at Phase 0 and evolved additively only. This is the one-way door whose retrofit cost dominates everything else: from Phase 4 onward the envelope is persisted in the ground-truth ledger, pinned in golden-seed fixtures, keyed into Kafka, served on REST/WS, and parsed by user pipelines; changing its shape after that touches every layer of the platform and every user's code simultaneously.

- **Status:** Accepted — review-blocking (one-way door)
- **Date:** 2026-06-10
- **Decides for:** the event model (D5); every producer, store, channel, and consumer of events, in every phase, forever within `/api/v1`

## Context

The forces:

- **One contract, many surfaces.** The envelope is the published language between Generation, Chaos, and Delivery (domain model §1), and the requirements demand channel expansion (REST/WS → Kafka/webhooks → S3/Iceberg/CDC export) without rework. The only way "a consumer migrating REST → Kafka changes transport code only" can be true is if the envelope is identical everywhere and fixed early.
- **Teaching requires specific fields.** Dedup labs need a stable idempotency key; watermark labs need event time vs delivery time as *separate, independently stamped* fields; ordering labs need a per-shard sequence; CDC labs need Debezium-shaped `op`/`before`/`after` from day one ("CDC support from day one" — honored at the contract level by freezing the CDC shape here, per ADR-0012).
- **Determinism vs UUIDv7.** ADR-0008 requires byte-identical sequences regardless of wall pacing; naive UUIDv7 embeds wall-clock milliseconds. The id scheme must be pinned before the first event is generated.
- **The panel's clock-domain gap.** All three proposals had `occurred_at` vs `emitted_at`, none defined the clock domain under a speed multiplier — "is a 30-min-late event 30 simulated or 30 wall minutes late?" An ambiguous answer frozen into the envelope would corrupt every time-based exercise.
- **Tenancy attribution** must be parseable without payload knowledge, at the broker and in every sink (ADR-0002).

## Decision

1. **Envelope `1.0` is frozen at Phase 0**; [../03-domain/event-model.md](../03-domain/event-model.md) §2.1 is the normative field catalog. The 20 delivered fields: `envelope_version`, `event_id` (UUIDv7), `workspace_id`, `stream_id`, `shard_id`, `scenario_slug`, `manifest_version`, `event_type`, `schema_ref {subject, version}` resolving into the registry (ADR-0010), `sequence_no` (gapless per `(stream_id, shard_id)` on the canonical stream, INV-GEN-7), `partition_key` (`{workspace_id}:{stream_id}:{entity_type}:{entity_key}` — workspace-prefixed per ADR-0002, the Kafka key per ADR-0005), `occurred_at`, `emitted_at`, `actor_id`, `session_id`, `entity_refs`, `correlation_id`, `causation_id`, `op`, `payload`. All 20 keys are always present; consumers ignore unknown additions.
2. **Deterministic UUIDv7:** `event_id` timestamp bits encode `occurred_at` (simulated) milliseconds; random bits come from the stream's seeded PRNG — ids are reproducible (INV-GEN-3), k-sortable in event time, and the only correct dedup key (event-model §2.2.1).
3. **Clock-domain rules (the gap, closed):** `occurred_at` is *always* virtual-clock business time; `emitted_at` is *always* wall-clock delivery time; neither is ever derived from the other. Chaos lateness is realized in delivery (wall) time — only `emitted_at` moves, never `occurred_at` (INV-CHA-6) — while temporal chaos *parameters* are specified in simulated time and converted by the speed multiplier (`wall_delay = simulated_delay / k`), so an exercise keeps its event-time meaning at any multiplier (event-model §3.4, with normative worked examples). Every time-shaped quantity in the platform is assigned a clock domain in the reference table at event-model §3.5.
4. **CDC rides the same envelope:** `op` ∈ `c`/`u`/`d`/`r` (closed enum, frozen) discriminates; a CDC event's `payload` is the Debezium-shaped sub-envelope (`before`/`after`/`op`/`ts_ms`/`source` incl. `source.ts_ms` = simulated change time and `source.entity_version` = per-entity total order), frozen with the envelope (ADR-0012, event-model §4).
5. **Internal labels ride, then strip.** Ground-truth/chaos metadata travels in the internal-only `_df` block on the ledger and internal Kafka, and is stripped by every sink at ingestion via one shared function; keys with the `_df` prefix are reserved at every nesting level, and a permanent CI scan of every channel's output enforces the boundary (INV-DEL-2, SB-1…SB-4). Ground truth reaches users only through the answer-key API (ADR-0017).
6. **Evolution policy:** additive-only minor bumps — add an optional field, never remove/rename/retype/re-mean anything, never extend a closed enum (EV-1…EV-7). There is no envelope `2.0` within the lifetime of `/api/v1`; a breaking change would require a new API major version, WS subprotocol, and topic generation simultaneously — stated so the cost is visible and the change is effectively never made. Additions require a superseding ADR referencing this one.

The load-bearing field groups and the exercise class each one carries:

| Field group | Fields | What breaks without it |
|---|---|---|
| Identity & dedup | `event_id` (deterministic UUIDv7) | Dedup labs (E1), at-least-once reasoning, golden replay |
| Tenancy & routing | `workspace_id`, `partition_key`, `stream_id`, `shard_id` | Broker-level isolation (ADR-0002), per-key ordering |
| Canonical order | `sequence_no` per `(stream_id, shard_id)` | Out-of-order restoration (E3), gap-vs-missing reasoning |
| Dual time | `occurred_at` (virtual) / `emitted_at` (wall) | Watermark/late-data labs (E2), backfill, speed multiplier |
| Schema lineage | `schema_ref`, `manifest_version`, `envelope_version` | Drift detection (E5), evolution exercises (Phase 10) |
| Causality | `correlation_id`, `causation_id`, `actor_id`, `session_id`, `entity_refs` | Funnel joins, saga tracing, CDC↔business joins |
| CDC | `op` + Debezium sub-envelope in `payload` | SCD2 (E4), CDC-from-day-one at the contract level |

## Alternatives considered

- **CloudEvents 1.0** as the envelope. Standardized, tooling exists. Rejected: CloudEvents has no first-class `sequence_no`, `partition_key`, dual-timestamp, tenancy, causality, or CDC discriminator — every load-bearing DataForge field would live in an extension profile we would own anyway, while ceding control of serialization details that byte-identical golden replay and canonical ordering depend on. Mapping the frozen envelope *to* CloudEvents at some future sink remains a sink-level adapter concern, not an envelope change.
- **Minimal envelope** (`event_id`, `event_type`, timestamp, `payload`) with the rest inside payloads per scenario. Rejected: tenancy attribution, dedup, ordering, and the strip boundary must be evaluable by every sink and the broker *without* payload knowledge; pushing them into scenario-defined payloads makes every guarantee per-scenario and unverifiable, and breaks the cross-channel uniformity contract.
- **Avro/Protobuf with the Confluent wire format.** Rejected for the contract: JSON is the user-facing teaching surface — students `curl` the cursor API and read events by eye; MVP deliberately has no Confluent SR (ADR-0010); and binary encoding at the Phase-12 external Kafka sink is achievable later as a sink-level mapping of the same logical envelope. Choosing Avro now would couple learners' first contact to schema-registry tooling the consumption model defers.
- **Single wall-clock timestamp; lateness modeled by re-stamping.** Rejected outright: it destroys the event-time/processing-time distinction that watermark, late-data, and SCD2 exercises exist to teach. The immutability of `occurred_at` under chaos is the teaching point.
- **Freeze later — iterate the envelope during MVP phases.** Rejected: every phase from 4 onward persists ledger partitions, golden fixtures, buffer rows, and user-visible documentation against the envelope; the retrofit cost compounds per phase. Phase 0 freeze with additive-only evolution is the cheapest point of maximal leverage.

## Consequences

### Positive

- Cross-channel uniformity is a testable property (the cross-channel contract suite), making the Phase-12 channel expansion a transport exercise.
- Chaos is gradable to the event: canonical truth (`sequence_no`, `occurred_at`) survives every injection; every delivered deviation is a recorded injection (INV-G-3).
- Deterministic ids + canonical serialization (event-model §2.4) make golden-seed replay byte-exact — the permanent CI anchor for INV-G-4.

### Negative

- Rigidity is the point but it costs: 20 mandatory keys add roughly 0.5–0.7 KiB per event before payload — accepted as teaching clarity over wire thrift, bounded by B-12 (payload ≤ 64 KiB, envelope ≤ 96 KiB).
- Deterministic UUIDv7 deviates from RFC 9562's wall-clock intent; documented deliberately (event-model §2.2.1) so the choice never surprises an implementer.
- Mistakes in `1.0` can only be deprecated-in-docs, never removed (EV-3) — the freeze makes this review gate the last cheap chance to catch them.

### Follow-ups

- Phase 3: machine-readable envelope JSON Schema as a CI artifact, golden-fixture-tested against the event model (the document wins on discrepancy).
- Phase 4: golden-seed fixtures pin canonical serialization; Phase 5 onward: per-channel `_df` strip scan in CI (SB-3).
- Any envelope addition follows EV-6: superseding ADR → event-model §2.1 update → schema artifact regeneration → fixture update.
