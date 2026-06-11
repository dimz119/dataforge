# DataForge — MVP vs Future Roadmap

**Deliverable:** D20

This document draws the MVP cut line, states the single rule that produced it, and proves the cut is safe: a per-capability table mapping everything deferred to the seam that makes its deferral rework-free, followed by the contract-freeze list — the five frozen contracts whose stability is what guarantees that post-MVP work is *additive adapters and instances*, never retrofits. The MVP is Phases 0–11, with the GA tag applied at the end of Phase 11 ([incremental-roadmap.md](incremental-roadmap.md) §6); per-phase scope lives in [phases/](phases/README.md); terminology follows [../03-domain/domain-model.md](../03-domain/domain-model.md).

---

## 1. The cut rule

> **Every one-way-door seam ships in the MVP; everything that is "another instance through an existing seam" is deferred.**

The rule, applied as a test on any capability:

| Question | If yes | If no |
|---|---|---|
| Would deferring this capability force a later change to a frozen contract (envelope, manifest schema, sink interface, registry subjects, tenancy model) or to the pipeline shape (Behavior → ledger → Chaos → Delivery)? | **It is a seam. It ships in the MVP** — even when no MVP user consumes it directly (e.g. the internal Kafka backbone at Phase 1, the CDC envelope shape at Phase 0). | It is an **instance**. It is deferred, and the table in §3 names the existing seam it will pass through — adding it later is new adapter/instance code plus configuration, with zero changes to generation, chaos, or the contracts. |

Two corollaries the panel fixed explicitly:

- **C-1 — Chaos and CDC are inside the MVP.** Chaos is the requirements' stated key differentiator — an MVP without it is "faker with a websocket" and validates nothing the product claims. CDC honors "CDC from day one" at the contract level from Phase 0 (the Debezium-shaped sub-envelope is frozen in [../03-domain/event-model.md](../03-domain/event-model.md) §4) and at the feature level inside the MVP (Phase 8, ADR-0012). Schema evolution stays in because the registry exists from Phase 3 and drift chaos depends on it — cutting it would save little and break the teaching story's coherence.
- **C-2 — The seam is proven, not assumed.** Phase 12 exists partly as an architectural proof: its exit criterion requires the external-Kafka and webhook diff to be confined to delivery adapters and configuration, with zero changes in behavior/chaos/runner code ([phases/phase-12-delivery-expansion.md](phases/phase-12-delivery-expansion.md)). If that criterion cannot be met, the cut rule was violated somewhere and the violation is treated as a release-blocking defect, not an accepted cost.

---

## 2. What the MVP is

MVP = Phases 0–11. The feature-by-phase inventory is owned by [../01-product/prd.md](../01-product/prd.md) §6; the one-line version: full tenancy and auth stack, declarative scenario runtime with the 8-entity e-commerce manifest, deterministic generation with virtual clock and backfill, streaming through the internal Kafka backbone to REST cursor + WebSocket consumption, the complete console, CDC, all 7 chaos modes with the instructor answer key, schema evolution v1→v2→v3, and production GA on Fly.io at a demonstrated ≥ 5k aggregate TPS with quotas, observability, and runbooks.

MVP consumption boundary (binding, user-confirmed — normative statement in [../02-architecture/system-architecture.md](../02-architecture/system-architecture.md) §4): users pull from hosted DataForge over the internet via cursor-based REST and WebSocket with an API key; DataForge's internal Kafka is invisible server-side infrastructure; bridging events into the user's own Kafka is their exercise (guide G1). Hosted per-workspace topics and webhooks arrive in Phase 12.

---

## 3. Deferral table

Each row names the capability, where it stands in the MVP, when it lands, and — the load-bearing column — the **seam that makes the deferral safe**: the already-shipped contract through which the capability arrives as an instance.

| Capability | MVP (phase) | Post-MVP (phase / backlog) | Seam that makes deferral safe |
|---|---|---|---|
| **External Kafka** — hosted per-workspace topics, SASL/SCRAM + ACLs | Contract frozen ([../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) §7): topic naming, partition budget, credential provisioning, consume-only ACLs. Seam ships Phase 5 | **Phase 12**, executing the managed-Kafka migration trigger (ADR-0015) | `DeliveryChannel` interface + every-channel-is-a-Kafka-consumer-adapter (ADR-0005); identical delivered envelope on every channel (event-model §6); broker endpoints are config (`KAFKA_BOOTSTRAP_SERVERS`) |
| **Webhooks** — HMAC-signed, retries + DLQ, delivery logs | Contract frozen ([../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) §8): request shape, HMAC-SHA256 signature, retry schedule, idempotency key = `event_id` | **Phase 12** | Same `DeliveryChannel` seam; `SinkBinding` aggregate already models sink type `webhook` (domain model §2.8) |
| **S3 / Iceberg / CDC export** to user-provided storage | Contract frozen now ([../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) §9): file/commit semantics, exactly-once-per-committed-file, `occurred_at`-window partition layout, Iceberg snapshot semantics, Debezium-format CDC JSONL layout | **Post-12 backlog** (first delivery channel after Phase 12; ordered by demand from the analytics-engineer persona) | `object_export` sink type through the same `DeliveryChannel` seam; the per-channel guarantee row for it is already in the frozen event-model §6 table, so consumers' expectations are pre-committed |
| **AI-generated scenario manifests** ("create a food delivery platform") | Validation contract ships in MVP: manifest JSON Schema v0 + semantic checks (resource bounds, probability sums, reachability) + dry-run budget + the AI slot-in contract and untrusted-manifest threat model ([../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §8, §12, §13) | **Post-12 backlog** (an LLM emitting a manifest that must pass the same three-layer validation as any human-authored one) | Scenarios are declarative manifests interpreted by a generic runtime (ADR-0003); workspace-visible scenarios are already a tenant-owned catalog concept (INV-CAT-6) — AI generation is a new *author*, not a new *contract* |
| **Additional scenarios** (SaaS, rideshare, IoT, FinTech, food delivery, healthcare, AdTech, …) | The seam: manifest grammar proven expressive by the full 8-entity e-commerce manifest written at Phase 0 and executed by Phase 8; zero scenario logic in core (permanent CI grep guard from Phase 3) | **Post-12 backlog**, demand-ordered; each is a YAML file under `backend/catalog/builtin/{slug}/` plus registered v1 schemas | ADR-0003: new scenario = new manifest data validated against the frozen schema; the runtime, registry derivation, chaos, and delivery are scenario-agnostic by construction |
| **100k TPS substrate scaling** (managed Kafka, partition rungs, runner fleets) | ≥ 5k aggregate TPS demonstrated at GA (Phase 11) plus the published capacity-arithmetic staircase to 100k ([../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md)); runner sharding model (per-shard leases, shard-pinned `sequence_no` scope) ships Phase 11 | **Trigger-based backlog**: each rung executes when its named bottleneck approaches (the staircase names bottleneck + remedy per rung); managed-Kafka migration on the ADR-0015 trigger | Sharding semantics are frozen in envelope 1.0 (`shard_id`, per-shard `sequence_no`); topic generations (`df.delivery.events.v2`) handle partition growth without remapping keys; stateless web/ws tiers and lease-based runners scale by count, not redesign |
| **Multi-region / 99.9% availability** | Stated honestly: single-region, single-broker MVP cannot meet 99.9%; SLO definitions, error budget, and the availability roadmap ship at Phase 11 ([../02-architecture/observability.md](../02-architecture/observability.md) §7) | **Post-GA roadmap**: managed Kafka → HA Postgres → multi-region, in that order (eliminates platform-wide failure domains stepwise, system-architecture §9.2) | SLOs and error budget are defined against the frozen control-plane/data-plane split; every process is stateless or lease-recoverable, so adding replicas/regions changes deployment topology, not architecture |
| **Confluent Schema Registry mirroring** | Subject naming frozen Confluent-compatible now (`{scenario_slug}.{event_type}`, INV-REG-1); mirroring procedure decided ([../04-engines/schema-registry.md](../04-engines/schema-registry.md) §13) | **Phase 12+** (lands with/after the external Kafka channel, where external consumers first need it) | ADR-0010: because subjects are Confluent-compatible from the first registration, mirroring is a mechanical export — no renaming, no version renumbering, no consumer migration |
| **Classroom self-host mode** (instructor-run DataForge on their own infrastructure) | **Not MVP, and not Phase 12** — explicitly listed here as a *possible future mode* so nobody mistakes compose-parity for a commitment. The MVP product is hosted multi-tenant SaaS only | **Unscheduled backlog**; would be scoped by its own ADR if demand materializes | One image / many commands (deployment D-2), config-only environment differences (D-3), and dev-prod topology parity (D-1) mean the entire platform already runs from `docker compose up` — the technical seam exists; what is deliberately *not* built in MVP is licensing, update distribution, and support tooling |
| **Billing & self-serve plan changes** | Plan tiers and quota numbers decided now (PRD §7); quota *enforcement* ships Phase 11; every workspace runs on an implicit Free-tier quota row until then | **Post-GA backlog** (payment integration, plan upgrade/downgrade flows) | The `quotas` schema and `QuotaPolicy` value object carry plan tiers from Phase 2; billing changes a row's tier — enforcement, statuses (`paused_quota`), and UI meters already exist |
| **OpenTelemetry adoption** | Structured JSON logs, Prometheus-style metrics, frozen metric/alert names ship Phase 11 ([../02-architecture/observability.md](../02-architecture/observability.md)) | **Post-GA backlog** per the observability doc's OTel adoption path | Log schema and metric names are stable identifiers; OTel is an exporter/transport change, not an instrumentation rewrite |
| **CDN for SPA assets** | SPA baked into the backend image, served by `web` via WhiteNoise with immutable-hashed assets (frontend-architecture §12.3) | **Post-GA backlog** | Hashed immutable assets are already cache-correct; a CDN is a serving-layer addition in front of unchanged build output |

Reading the table as a whole: **every "post-MVP" entry names a seam that is itself an MVP deliverable.** That is the cut rule operating in reverse — the MVP's job is to ship and freeze the seams; the future's job is to push instances through them.

---

## 4. The contract-freeze list

These five contracts are why the deferrals in §3 need no rework. Each was frozen at Phase 0, has a normative owner, an explicit evolution rule, and a CI mechanism that makes accidental drift a build failure rather than a discovery.

| # | Frozen contract | Normative owner | Frozen content | Evolution rule | CI proof |
|---|---|---|---|---|---|
| F-1 | **Canonical event envelope `1.0`** (incl. Debezium-shaped CDC sub-envelope and clock-domain rules) | [../03-domain/event-model.md](../03-domain/event-model.md) (ADR-0004, ADR-0012) | The 20 delivered fields, types, null rules, canonical serialization order; `op` enum closed at `c/u/d/r`; `occurred_at` = simulated, `emitted_at` = wall; `_df` strip boundary | Additive-only minor bumps (EV-1…EV-7); no envelope 2.0 within `/api/v1`'s lifetime | Envelope JSON Schema CI artifact + exact field-set pin test (an unannounced 21st field fails the build); permanent strip-boundary scan SB-3 on every channel |
| F-2 | **Manifest JSON Schema v0** | [../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §9 (ADR-0003) | The full grammar: entities, generator vocabulary, relationships, event types, state machines, preconditions, CDC config, chaos defaults; semantic-check catalog (MAN-*); resource bounds (B-01…B-17) | `manifest_schema` grammar versioning (§9.3): a breaking grammar change is `v1` with dual-version loader support — `v0` manifests are interpreted forever | Schema is a versioned repo artifact and CI job from Phase 3; the builtin e-commerce manifest re-validates through all three layers on every build |
| F-3 | **`DeliveryChannel` sink interface** | [../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) §3 (ADR-0005) | `deliver(batch)` semantics, cursor/ack contract, backpressure signal, error classification, `strip_internal` at ingest, per-channel guarantee table (incl. the frozen future-channel rows) | New channels implement the interface; interface changes require a superseding ADR referencing ADR-0005 | Sink conformance suite (§3.7) every sink must pass; cross-channel contract tests (XCH) assert identical envelopes across channels; import-linter confines sink code to delivery adapters |
| F-4 | **Registry subjects & compatibility** | [../04-engines/schema-registry.md](../04-engines/schema-registry.md) (ADR-0010) | Subject grammar `{scenario_slug}.{event_type}` / `{scenario_slug}.cdc.{entity_type}` (Confluent-compatible, INV-REG-1); immutable monotonic versions (INV-REG-2); `BACKWARD_ADDITIVE` enforcement (INV-REG-3) | New versions only, additive only; compatibility mode changes are out of scope for `/api/v1` | Compat-checker unit + property oracle (PROP-SM-3); drift-⊆-registered-next-version test (CHD-6); subject-grammar unit tests |
| F-5 | **Tenancy model** | ADR-0002; enforcement in [../06-quality/security-architecture.md](../06-quality/security-architecture.md) §4 | Workspace = tenant; non-null `workspace_id` on every tenant-owned row, envelope, Kafka message key (`partition_key` prefix), Redis key, and counter; shared schema + scoped managers + CI guard + RLS | The carrier set only grows (any new surface must carry `workspace_id` before it ships); never schema-per-tenant | Permanent cross-tenant attack suite (TEN) from Phase 2; `tenancy.E001–E004` CI guard; RLS verified with the ORM bypassed |

What freezing buys, stated as the §3 guarantees:

- F-1 + F-3 ⇒ external Kafka, webhooks, and object export deliver **the same envelope** users already parse — a consumer migrating channels changes transport code only (event-model §6 invariant).
- F-2 ⇒ new and AI-generated scenarios are data passing existing validation — zero core change (ADR-0003's whole point).
- F-4 ⇒ Confluent SR mirroring is mechanical, and mid-stream evolution exercises keep working on every future channel.
- F-5 ⇒ every future surface (hosted topics, webhook endpoints, export buckets) inherits the same isolation walls instead of inventing new ones; the attack suite extends to each new surface as a permanent gate.

---

## 5. What is deliberately not promised

To keep the cut honest, these are explicitly **out of scope for both MVP and the named post-MVP items** unless a future ADR says otherwise:

| Not promised | Why it stays out |
|---|---|
| User-authored Python plugins or tenant-uploaded code | Violates ADR-0003's safety model; hooks are platform-code-only, value-generation-only, and banned in the reference scenario |
| Tenant access to the internal Kafka backbone, in any form | Consumption-boundary rule CB-1 ([../02-architecture/system-architecture.md](../02-architecture/system-architecture.md) §4): no tenant-issuable credential for the backbone exists in any phase — hosted topics (Phase 12) are a separate external surface |
| An envelope `2.0` | EV-5: a breaking envelope change would require a new API major version, WS subprotocol, and topic generation simultaneously; the cost is documented precisely so the change is effectively never made |
| Offset-based pagination, non-RFC-9457 errors, or a second API version | ADR-0014 fixes the conventions for the life of `/api/v1` |
| SSR / Node render tier for the console | ADR-0016: authenticated SPA, no SEO surface; revisiting requires superseding that ADR |

---

## 6. Ownership boundaries

| Concern | Owner |
|---|---|
| Phase sequencing, dependency graph, review gates | [incremental-roadmap.md](incremental-roadmap.md) |
| Per-phase scope and exit criteria (binding text) | [phases/README.md](phases/README.md) and the thirteen phase docs |
| Feature-by-phase inventory and plan/quota tiers | [../01-product/prd.md](../01-product/prd.md) §6–7 |
| Future-channel contract details (topics, HMAC, file/commit semantics) | [../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) §7–9 |
| AI-manifest slot-in contract and threat model | [../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §12–13 |
| Scaling staircase arithmetic and rung triggers | [../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md) |
| SLO definitions and the availability upgrade path | [../02-architecture/observability.md](../02-architecture/observability.md) §7 |
| Managed-Kafka migration trigger | ADR-0015, [../02-architecture/deployment-architecture.md](../02-architecture/deployment-architecture.md) |
