# ADR-0007 — Behavior engine: per-actor state machines over entity pools

**Deliverable:** D17

Event generation is the traversal of manifest-defined state machines by simulated actors over shared entity pools: transitions are selected probabilistically, paced by dwell-time distributions under intensity curves, gated by precondition guards, and every event is the consequence of a pool mutation. This warranted an ADR because it decides *where validity lives*: with guarded transitions over pools, an invalid sequence (a refund without a delivered order) is structurally impossible to generate — validity is a property of the generator's output space, not a filter applied to it — and that property is what every realism claim, soak test, and grading guarantee in the product rests on.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the behavior engine design (D7), the Generation context, the manifest's `state_machines`/`entities` semantics; Phases 4, 6, 8

## Context

The forces:

- **The requirement names the mechanism:** "events follow realistic workflows (Product Viewed → 20% Add To Cart → 40% Checkout → 70% Purchase … → Delivered → Review). Configurable transition probabilities, stateful actor/session simulation, no invalid sequences" — and referential integrity in *events*: "refund without an order = invalid, never emitted."
- **Realism is now a numeric contract.** PRD §4 fixes funnel probabilities (F1–F10), lifecycle latency distributions (L1–L8), and intensity curves with acceptance tolerances (±1 pp absolute or ±10% relative at n = 10,000). The generator must *realize* configured rates measurably, not just emit plausible-looking rows.
- **Validity must hold at any volume.** INV-G-2 demands zero referential violations on a 1M-event soak (Phase 8 exit criterion) and the PRD's counter-metric is "violations in production: 0, ever." A statistical claim cannot meet an absolute bar; only a structural guarantee can.
- **Throughput and locality:** per-event cost must be microseconds (ADR-0006); actor and session state must live in one runner's memory, and pause/resume must preserve in-flight funnels with zero sequence gaps (Phase 6 exit criterion).
- **CDC consistency** (ADR-0012) requires a mutable substrate: `before`/`after` images only exist if entities have authoritative current state somewhere.

## Decision

1. **Per-actor state machines, shapes from the manifest** (ADR-0003): exactly one `session` machine bound to the scenario's actor entity, spawned by the arrival process; 0–9 `lifecycle` machines spawned when an entity of their bound type is created. Transition probabilities, dwell distributions, guards, and effects are manifest data, workspace-overridable within declared `override` bounds (scenario-plugin-architecture §6, §11).
2. **Entity pools are the source of truth** (per stream × entity type): hot state in Redis, periodic Postgres snapshots; every pooled entity carries an `entity_version` incremented per mutation. All events — business and CDC — derive from pool mutations executed by transition effects (`create`/`update`/`adjust`/`delete`), so an event's references resolve to entities that existed at its `occurred_at` (INV-GEN-1).
3. **Guards make invalid sequences unrepresentable** (INV-GEN-2). A transition fires only when its manifest-declared preconditions hold, evaluated against the pools over declared relationships (e.g. `refund_requested` requires a shipment with `status ∈ {delivered, lost}`). A failed guard falls through to the state's remainder policy — never a re-draw, which would silently redistribute probability mass (scenario-plugin-architecture §6.2 rule 3).
4. **Dwell and timeouts pace in simulated time:** the selected transition's dwell distribution is sampled at selection and elapses on the virtual clock (ADR-0008); state-level timeouts compete (PRD L1's "30 min, else `order_cancelled`"). Lifecycle latencies (L1–L8) are dwell parameters, nothing more.
5. **Intensity curves modulate session arrival, not event pacing:** effective arrival rate = `(target_tps / mean_events_per_session) × diurnal(hour) × weekly(day)`, with curves renormalized to mean 1.0 so shape changes never change average throughput (PRD §4.3); `mean_events_per_session` comes from the manifest dry run (MAN-D6xx).
6. **Checkpointable by construction:** shard state — actor/session machine positions, dwell timers, pool cursors, RNG positions, virtual-clock position, last `sequence_no` — serializes to a Checkpoint every 30 s and on pause/stop, enabling lease failover and pause/resume with in-flight funnels intact (ADR-0006, domain model §2.6).
7. **Bounded against adversarial manifests:** actors shard by hash of their PK-1 key (ADR-0006); traversals hard-cap at 10,000 transitions (B-13 backstop); live working sets cap per B-09 with terminal-entity archival ([../04-engines/behavior-engine.md](../04-engines/behavior-engine.md)).

How each clause of the PRD realism contract maps onto an engine mechanism:

| Realism contract (PRD §4) | Engine mechanism | Verified by |
|---|---|---|
| Funnel probabilities F1–F10 (± tolerance) | Per-state transition probabilities + remainder rule | Statistical suite, n = 10,000 sessions |
| Lifecycle latencies L1–L8 (median/p95/hard bound) | Dwell distributions + competing state timeouts | Latency-distribution tests, n = 10,000 transitions |
| Diurnal/weekly shape (§4.3) | Intensity curves on session arrival, renormalized mean 1.0 | 30-day backfill shape tests (Phase 8) |
| Structural invariants (§4.4) | Precondition guards + pool mutations (INV-GEN-1/2) | 1M-event property soak, zero violations |
| Byte-identical reproducibility | Seeded sub-streams + virtual clock (ADR-0008) | Golden-seed replay in CI (INV-G-4) |

## Alternatives considered

- **Generate-then-filter:** emit candidate events freely, validate referential integrity afterwards, drop invalid ones. Rejected: dropping distorts the configured funnel rates unpredictably (the tolerances in PRD §4.1 become untunable); "no invalid sequences" degrades from a guarantee into a sampling claim that cannot meet INV-G-2's absolute bar; and validation work is wasted compute exactly where throughput matters most.
- **Stateless per-table synthesis with FK sampling** (the Faker/Mockaroo model). Rejected: this is the category the PRD positions against — no causality, no sessions, no time structure; "refund requires a delivered order" is inexpressible when rows are generated independently; CDC before/after images have no substrate.
- **Markov chains over event types without entity pools** (probabilities over event-type sequences only). Rejected: sequences *look* plausible but references dangle — a `payment_authorized` names no real order, inventory is unconstrained and goes negative, and ADR-0012's one-mutation-two-views model is impossible with no state to mutate.
- **A general discrete-event-simulation framework** (SimPy-style coroutine processes per actor). Rejected: coroutine continuations do not serialize across processes, which lease failover requires (INV-STR-2); determinism under restore would mean owning the framework's scheduler anyway. The manifest-interpreted machine *is* a purpose-built DES kernel whose entire state is checkpointable data.
- **Scripted traces** (hand-authored event sequences replayed with jitter). Rejected: no configurable probabilities, no workspace overrides, no emergent contention on shared entities (inventory), and authoring effort scales with scenario count — the opposite of ADR-0003's economics.

## Consequences

### Positive

- INV-G-2 holds structurally at any volume: the 1M-event soak and the "0 violations, ever" counter-metric verify an impossibility, not a tendency.
- Configured-vs-realized rates are a well-posed statistical test (testing-strategy's tolerance suite), because nothing downstream drops events.
- Pause/resume, crash failover, and stop/restart all reduce to checkpoint restore (Phase 5/6 kill-test exit criteria); CDC emerges from the same mutations at zero additional modeling cost (ADR-0012).

### Negative

- Hot state costs memory: actors, sessions, dwell timers, and pools bound stream size (B-08/B-09, ≈ 244 MiB Redis worst case per stream); the scaling staircase must budget it ([../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md)).
- Realized rates condition on guard pass — a heavily guarded transition realizes below its configured probability. This is inherent and correct (guards are preconditions, not suggestions); the dry run reports realized rates and W-D610 flags guard-starved transitions so authors see the divergence before publish.
- Guard evaluation needs relationship-indexed pools (O(1) exists-checks) and effect ordering must match CDC ordering (R-CDC-2) — engine complexity concentrated in one component, mitigated by the manifest grammar keeping guards/effects small (B-07).

### Follow-ups

- [../04-engines/behavior-engine.md](../04-engines/behavior-engine.md) owns the tick loop, pool index design, checkpoint format, working-set archival, and the arrival-rate conversion.
- Phase 4 ships the v1 interpreter with golden-seed and 1M-event invariant tests; Phase 6 ships checkpointed pause/resume and dynamic TPS; Phase 8 ships intensity curves, the full 8-entity funnel, and the statistical tolerance suite.
