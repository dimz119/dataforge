# ADR-0009 — Staged pipeline: Behavior → ground-truth ledger → Chaos → Delivery

**Deliverable:** D17

The data plane is a strict four-stage pipeline: the behavior engine always produces a clean, referentially valid canonical stream persisted to an append-only ground-truth ledger; the chaos engine is a seeded, ordered, composable transform stage *after* the ledger; delivery sinks consume only the post-chaos stream. This is a one-way door because the stage order decides where truth lives: once the ledger, the answer key, the injection records, and every statistical test are built against "canonical truth exists upstream of corruption," moving chaos anywhere else invalidates all of them simultaneously — and an MVP built without the ledger can never recover the ground truth it failed to record.

- **Status:** Accepted — review-blocking (one-way door)
- **Date:** 2026-06-10
- **Decides for:** the data-plane pipeline shape (D2, D7, D8); the chaos engine's position and contract (D8); the answer key's substrate (ADR-0017); Phases 4 and 9

## Context

The forces:

- **Chaos is the stated key differentiator**, and its value proposition is *gradable* mess: the instructor must know exactly which events were duplicated, delayed, suppressed, or mutated (PRD §1, ADR-0017). Grading requires a recorded truth that the corruption demonstrably deviates from.
- **Truth separation is a product invariant:** the structural realism invariants (PRD §4.4) must hold at any volume *while* delivery is being corrupted — "chaos corrupts delivery truth, never business truth" (INV-G-3).
- **Seven modes must compose, toggle at runtime, and test independently:** the Phase 9 exit criteria are per-mode statistical tolerances (5% duplicates ⇒ 5% ± 1% over 50k events), all toggle combinations crash-free, and answer-key counts matching injections exactly.
- **The panel gap this ADR closes:** all three proposals implemented late arrival as a delayed re-publish queue, and none specified what happens to scheduled re-emissions when the stream is paused, stopped, resumed, or its runner fails over. Undesigned, this silently deflates realized lateness rates and corrupts answer-key counts.
- The ledger additionally serves Phase 4's batch value (JSONL downloads) and E7's canonical aggregates — the same artifact, three consumers.

## Decision

1. **Stage order, strict and final:** behavior engine → **ground-truth ledger** (append-only, time-partitioned Postgres, full internal envelope including `_df` labels; every canonical event is persisted before the chaos stage may read it, INV-GEN-5) → **chaos transform** → internal Kafka delivery topics (ADR-0005) → sinks (which never read the ledger, pools, or pre-chaos data, INV-DEL-1).
2. **Chaos is an ordered, composable, seeded stage pipeline** over the canonical stream, normative mode order `missing → duplicates → corrupted_values → nulls → schema_drift → out_of_order → late_arriving` (selection before mutation before displacement; full rationale in [../04-engines/chaos-engine.md](../04-engines/chaos-engine.md)). All draws come from the `chaos` sub-seed: identical `(seed, chaos configuration)` yields identical injections (INV-CHA-2, ADR-0008), and enabling chaos never perturbs the canonical sequence.
3. **Runtime-toggleable per stream:** per-mode enable/rate/params are desired state (PIN-3), picked up next tick, rate-capped at 0.5 (B-16), every change audit-logged. Chaos config is the one live-mutable engine surface precisely because it sits downstream of truth.
4. **The doctrine — delivery truth vs business truth (INV-CHA-1, INV-G-3):** chaos reads only ledger output and writes only to delivery topics. It never mutates entity pools, the ledger, `occurred_at`, `sequence_no`, or causality fields; per mode it may duplicate, suppress, displace, shift `emitted_at`, or mutate payload fields — every delivered deviation from the ledger is a recorded injection.
5. **Record before publish (INV-CHA-4):** an InjectionRecord is persisted before the affected instance is published or suppressed, so the answer key matches what was delivered to the event — the Phase 9 "counts exactly match" exit criterion is a consequence, not an aspiration.
6. **The late-arrival buffer is persistent, with pinned lifecycle semantics** (the gap, closed): entries `{event ref, due_at (wall), state ∈ pending|emitted|discarded}`. Pending re-emissions **survive pause** (held, not dropped; entries due during a pause emit promptly on resume, with realized vs configured delay recorded on the injection record) and **survive runner failover** (the new lease holder adopts pending entries). **Stop applies the stream's OnStopPolicy** — `discard` (default; entries marked `discarded` on their records) or `flush` (immediate emission during the stopping phase). Stream deletion removes pending entries (domain model T14). Delay *parameters* are simulated time, *realization* is wall time, per the frozen rule in [../03-domain/event-model.md](../03-domain/event-model.md) §3.4.
7. **Mode-boundary constraints from sibling contracts:** `schema_drift` injects only registered next-version fields (INV-REG-5, ADR-0010) and never mutates CDC `before` images (R-CDC-6); chaos at any rate never crosses workspaces (INV-CHA-7).

The late-arrival buffer's lifecycle semantics, exhaustively (the table the panel proposals lacked):

| Stream event | Pending re-emissions | Recorded as |
|---|---|---|
| Pause | Held, never dropped; entries due during the pause emit promptly on resume | Realized wall delay alongside configured simulated delay |
| Resume | Pending entries intact (INV-CHA-5); scheduling continues | — |
| Runner crash / failover | New lease holder adopts pending entries (persistent store, not runner memory) | — |
| Stop, `OnStopPolicy: discard` (default) | Dropped during the stopping phase | `state = discarded` on each InjectionRecord |
| Stop, `OnStopPolicy: flush` | Emitted immediately, not at `due_at` | Realized early emission on each record |
| Delete (T14) | Removed with the stream's buffer rows and checkpoints | Records follow retention, not deletion |

## Alternatives considered

- **Chaos inside the behavior engine** (generate corrupted output directly). Rejected: no clean truth ever exists, so the answer key becomes reconstruction instead of record and grading is approximate; toggling a mode would change the canonical sequence itself, destroying the determinism unit (a chaos-off replay could not verify a chaos-on stream); each mode entangles with generation logic instead of being an independently testable transform.
- **Chaos at the delivery sinks** (each channel corrupts independently). Rejected: 7 modes × N channels implementations; the cross-channel uniformity contract (event-model §6 — same delivered instance, same envelope everywhere) breaks by construction; injection recording fragments per channel.
- **No persisted ledger** — chaos as an in-memory transform with injections logged. Rejected: the canonical sequence itself is answer-key content (E3's byte-comparable re-sort, E7's canonical aggregates) and the substrate for batch downloads (Phase 4); a crash between generation and injection logging loses truth unrecoverably; "the ledger is always clean" is the cheapest possible statement of INV-G-3 to test.
- **An ephemeral in-memory delay queue for late arrivals** — what all three panel proposals implied. Rejected per the gap analysis: pending re-emissions vanish on pause or crash, silently lowering realized lateness below configured rates and breaking answer-key exactness (INV-CHA-4). Persistence with pinned pause/stop/failover semantics is the design answer, and Phase 9's exit criteria test it directly (a paused stream resumes with pending re-emissions intact).
- **Corrupt at read time** (store canonical in the buffer; apply chaos when a client reads). Rejected: WS and future sinks do not read the REST buffer, so chaos would be REST-only; replay stability (INV-DEL-3 — re-reading a cursor returns identical events) forbids per-read corruption; late arrival cannot be expressed at all in a pull model.

## Consequences

### Positive

- Every chaos mode is an independently testable pure transform with its own statistical tolerance test; composition is defined by the fixed stage order rather than emergent interference.
- Grading is exact, to the event: ledger + InjectionRecords = answer key (ADR-0017); a chaos-off stream is simply the canonical stream, which keeps first-run defaults safe (PRD §8 guardrail).
- The clean/corrupt boundary is also the test boundary: invariant suites run on the ledger, chaos suites on the delta — neither contaminates the other.

### Negative

- Every canonical event costs a ledger write before delivery: an ingest-throughput and storage tax (time-partitioned, 7-day default retention via partition drop; the write ceiling is a named rung in [../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md)).
- The late-arrival buffer makes chaos stateful infrastructure — persistence, wall-clock scheduling, and failover adoption — the most operationally complex part of an otherwise pure transform stage.
- No unrecorded entropy exists: even "missing" data is bookkept. Deliberate — the product sells *verifiable* mess — but it means chaos can never be a cheap fire-and-forget mutation.

### Follow-ups

- [../04-engines/chaos-engine.md](../04-engines/chaos-engine.md) owns per-mode config schemas, the stage-order rationale, and late-buffer mechanics; [../03-domain/database-schema.md](../03-domain/database-schema.md) owns ledger/buffer/InjectionRecord DDL and partitioning.
- Phase 4 ships the ledger with the first emission; Phase 9 ships all seven modes, the answer-key API, presets, and the statistical + lifecycle exit criteria (including the pause-with-pending-re-emissions test).
- The S3/Iceberg export contract in [../04-engines/delivery-channels.md](../04-engines/delivery-channels.md) inherits this boundary: exports are post-chaos delivered shape; canonical exports are an answer-key surface, never a delivery channel.
