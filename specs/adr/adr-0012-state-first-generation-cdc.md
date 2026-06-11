# ADR-0012 — State-first generation: business events and CDC derive from the same entity-pool mutations

**Deliverable:** D17

Business events and CDC events are two projections of one thing: a mutation of a pooled entity. The behavior engine mutates entity pools as transition effects; the business event expresses the transition, and each mutation of a CDC-enabled entity emits exactly one Debezium-shaped CDC event whose `before`/`after` images are the pool state around that mutation. This warranted an ADR because the alternative — generating the two streams separately — makes their consistency a statistical aspiration instead of a structural guarantee, and because the CDC payload shape is frozen with the envelope at Phase 0 (ADR-0004): it is the second-largest retrofit surface in the platform, and "CDC support from day one" is honored at the contract level from the first event ever emitted.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the CDC sub-envelope contract ([../03-domain/event-model.md](../03-domain/event-model.md) §4, frozen Phase 0); Generation's emission mechanics (INV-GEN-6); CDC feature delivery in Phase 8

## Context

The forces:

- **The requirement:** "CDC support from day one: INSERT/UPDATE/DELETE events (address changed, subscription cancelled, inventory updated)" — alongside business events, over the same simulated entities.
- **Consistency between the two views is the product's credibility.** A CDC feed that disagrees with the business stream — an `order_placed` with no `cdc.orders` insert, images that don't match payload values — reproduces exactly the incoherence fake-data tools suffer from and the PRD positions against. The Phase 8 exit criteria demand it structurally: no `u`/`d` before `c`, accurate images, consistency with the business stream, property-tested.
- **The SCD2 exercise (E4) is contract-sensitive:** dbt snapshots need full row images, a usable change column, and a per-entity total order; the answer key needs the exact mutation log. Debezium's `op`/`before`/`after`/`ts_ms`/`source` shape is the de-facto industry contract learners will meet in production, and its event-time/processing-time split (`source.ts_ms` vs `ts_ms`) maps exactly onto `occurred_at`/`emitted_at`.
- **Resolved disagreement on timing:** P1 shipped CDC after its MVP cut (violates "day one"); P3 shipped it at Phase 3 (front-loads scope before the core loop exists); P2 at Phase 8 pre-GA. Unified: the *shape* freezes at Phase 0 inside the envelope, *emission* ships at Phase 8 — inside the MVP, after the streaming core.

## Decision

1. **State-first:** the entity pool is the single substrate of record (ADR-0007). A transition's effects mutate pooled entities in declaration order; the business event and the CDC event(s) are projections of those mutations — there is no independent CDC generator and no independent business-event generator.
2. **One mutation, one CDC event (R-CDC-1, INV-GEN-6):** every mutation of a CDC-enabled entity emits exactly one CDC event whose `before`/`after` images equal the pool state immediately around the mutation. No mutation skipped, no CDC event without a mutation.
3. **Debezium-compatible sub-envelope, frozen at Phase 0** ([../03-domain/event-model.md](../03-domain/event-model.md) §4): envelope `op ∈ c/u/d/r` (closed enum) discriminates; the payload carries `before`/`after` row images conforming to the registry's `{slug}.cdc.{entity}` subject (ADR-0010), `ts_ms` (wall, ≡ `emitted_at`), and the `source` block — `connector: "dataforge"`, `db` = scenario slug, `table` = entity type, `source.ts_ms` (simulated, ≡ `occurred_at`), `source.seq` (≡ `sequence_no`), `source.entity_version` (the authoritative per-entity total order, gapless per instance), `source.tx_id` (the causing business event's id).
4. **Ordering and causality are pinned:** within a shard the causing business event is emitted first; its CDC events follow with consecutive `sequence_no`s in effect declaration order, sharing its `occurred_at` and `correlation_id` (R-CDC-2, C-4). CDC partition keys are always the mutated entity (PK-2 — Debezium's table-PK keying, not overridable), so per-entity CDC order converges on one Kafka partition even when mutations originate in different shards.
5. **Background mutations** (manifest-declared attribute drift with no business cause, e.g. E4's 0.5%/entity/day address change) emit CDC only, as chain roots with `causation_id`/`actor_id`/`tx_id` null (R-CDC-3); **snapshot `r` events** surface pre-seeded pool state once per CDC-enabled entity instance at stream start and at the head of backfill downloads (event-model §4.3).
6. **Structural guarantees:** no `u`/`d` is ever emitted before the entity's `c` or `r` within a stream (R-CDC-4, a permanent Phase 8 property test); per-entity CDC toggles are scenario-instance configuration pinned at stream start (no mid-stream enablement, so `r` rows occur only at feed head); chaos applies to CDC envelopes like any other, except `schema_drift` never touches `before` images (R-CDC-6).
7. **Timing:** the shape is frozen at Phase 0 — the envelope `op` field exists (null on business events) and the CDC schema subjects derive from manifests from Phase 3 — and emission ships at Phase 8, inside the MVP. "CDC from day one" is thereby honored at the contract level from event one and at the feature level within the MVP.

One mutation, two views — the requirement's own examples mapped onto the mechanism:

| Requirement example | Mutation source | Emission (per R-CDC-1/2/3) |
|---|---|---|
| Order placed decrements stock | `create orders` + `adjust inventory.stock` effects | `order_placed` seq *n* → `cdc.orders` `c` seq *n*+1 → `cdc.inventory` `u` seq *n*+2, shared `occurred_at`/`correlation_id` |
| Address changed | Background mutation, 0.5%/entity/day (E4) | `cdc.users` `u` only — chain root, `causation_id`/`actor_id`/`tx_id` null |
| Order cancelled | `update orders.status` effect on the cancelling transition | `order_cancelled` seq *m* → `cdc.orders` `u` seq *m*+1 |
| Pre-seeded catalog surfaced | No mutation — seeded pool state | `cdc.{entity}` `r` once per instance at stream start / backfill head |

## Alternatives considered

- **Real CDC capture** — keep entity pools in Postgres and run Debezium (or logical decoding) against them. Rejected: pools are Redis-hot for throughput and locality (ADR-0007); a connector per stream is unworkable at classroom scale; capture timing is nondeterministic, breaking INV-G-4's byte-identical replay; and it couples generation to connector operations — simulating a database in order to capture a simulation inverts the architecture for no fidelity gain (the emitted shape is Debezium-compatible either way).
- **An independent CDC generator** with its own change distributions. Rejected: consistency between the views becomes statistical, not guaranteed — precisely the failure class the product exists to not have; `before` images would be fabricated rather than observed; and every business-rule change would need mirroring in two generators forever.
- **CDC-first** (generate a change log; derive business events from it). Rejected: business events carry session, funnel, and causality semantics (`session_id`, dwell, correlation chains) that row deltas do not contain; the behavior engine's unit of meaning is the transition, and deriving "checkout_started" from row diffs is reconstruction of information the engine already had.
- **A custom, simpler CDC shape.** Rejected: learners must meet Debezium in production — Kafka Connect ecosystems, dbt patterns, and course material all speak it; the envelope already carries DataForge's own semantics, so the payload is exactly where industry compatibility belongs. The cost (a verbose `source` block) is bounded and itself teachable.
- **Timing alternatives, recorded:** P1's post-MVP placement violates the "day one" requirement; P3's Phase-3 placement front-loads CDC before streaming exists. Freezing the contract at Phase 0 and shipping emission at Phase 8 satisfies the requirement without distorting the phase plan (resolved disagreement, design panel).

## Consequences

### Positive

- The two views are provably consistent because they are projections of one mutation: R-CDC-1…R-CDC-7 are testable structural properties, not reconciliation jobs — the Phase 8 CDC consistency suite verifies impossibilities.
- SCD2 is gradable to the mutation: `entity_version` + `source.ts_ms` reconstruct exact validity intervals, and the answer key exposes the ground-truth mutation log (ADR-0017).
- Freezing the CDC frame inside the envelope at Phase 0 avoids the dominant retrofit cost: no ledger row, fixture, or user pipeline ever needs migrating to a CDC-capable shape.

### Negative

- CDC multiplies event volume: each business event may be followed by up to 8 CDC events (B-07 bounds the burst), and full row images make CDC payloads heavy (bounded by B-04 attribute caps and the B-12 64 KiB ceiling); per-entity toggles are the user's volume control.
- The `op` enum and the Debezium frame are frozen forever (EV-2): a mistake in the `source` block can only be deprecated in documentation, never removed — this review gate is the last cheap chance to catch one.
- Every mutation of a CDC-enabled entity pays image-capture cost even when no consumer reads the CDC feed; accepted, since the ledger needs the canonical record regardless and toggles default per manifest (`enabled_default`).

### Follow-ups

- [../03-domain/event-model.md](../03-domain/event-model.md) §4 is the normative shape; [../04-engines/behavior-engine.md](../04-engines/behavior-engine.md) owns mutation→emission mechanics and image capture; [../04-engines/schema-registry.md](../04-engines/schema-registry.md) owns `cdc.*` subject derivation (R-DER-1).
- Phase 8 ships emission, per-entity consumption filtering (R-CDC-7), and the consistency property suite; Phase 10 documents the SCD2-via-CDC exercise end-to-end.
- Any Phase-12 sink-level mapping (e.g. native Kafka tombstones for `d`) is owned by [../04-engines/delivery-channels.md](../04-engines/delivery-channels.md); the envelope `d` event remains the contract.
