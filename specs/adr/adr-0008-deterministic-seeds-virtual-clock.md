# ADR-0008 — Deterministic per-stream seeds + virtual clock with speed multiplier and backfill

**Deliverable:** D17

Every stream owns one seed from which all randomness derives through namespaced sub-seeds, and one virtual clock that stamps business time independently of wall time, supporting a speed multiplier and a backfill mode. This warranted an ADR because reproducibility is what converts DataForge from a noise generator into a teaching instrument — graded labs, golden-replay CI, and crash failover that continues the *identical* sequence all depend on it — and because determinism is a whole-system discipline that cannot be retrofitted onto an engine that ever consulted wall time or an unseeded RNG.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** all randomness and all business-time semantics in Generation and Chaos (D7, D8); the clock-domain rules in the event model (D5); Phases 4, 8, 9

## Context

The forces:

- **Grading and repeatability:** the instructor persona configures seed `4242` and the "same seed next semester reproduces the identical lab" (PRD §2.2). Chaos injections must reproduce too, or answer keys are single-use (ADR-0017).
- **Testing a generative system:** byte-identical golden-seed replay is the only cheap, permanent regression anchor for statistical machinery; without it every engine change needs fresh statistical analysis (INV-G-4 is a permanent CI gate).
- **Failover correctness:** a runner restored from a checkpoint must continue the exact canonical sequence (ADR-0006) or the stream forks and the ledger lies.
- **Time is the product's hardest realism axis:** lifecycle latencies are *days* (L3 median 2.5 simulated days, PRD §4.2). At wall pacing, funnel joins are untestable inside a class hour; analytics-engineer learners need 30 days of history that never streamed at all (E7).
- **Two pinned tensions, both closed elsewhere by this decision:** naive UUIDv7 embeds wall-clock milliseconds (resolved in [../03-domain/event-model.md](../03-domain/event-model.md) §2.2.1 per ADR-0004), and the panel's clock-domain gap — "is a 30-min-late event 30 simulated or 30 wall minutes late?" — is resolved by the simulated-parameter/wall-realization rule in event-model §3.4, which presupposes the clock model decided here.

## Decision

1. **One seed per stream**, client-supplied or generated at creation (domain model T1), immutable for the stream's life and never re-rolled across stop/restart (INV-STR-5).
2. **Namespaced sub-seeds**, derived as `HMAC(seed, namespace)` for the namespaces `values` (attribute generation, UUIDv7 random bits), `transitions` (machine selection draws), `pools` (entity keys, seeding, background-mutation scheduling), and `chaos` (injection selection, delays, mutations). Namespacing isolates consumption: enabling a chaos mode never perturbs the canonical sequence, because Chaos draws from its own stream of randomness.
3. **The determinism unit is `(manifest_version, seed, merged-config sha256)`** (PIN-1): identical inputs yield a byte-identical canonical sequence *and* identical chaos injections, independent of wall pacing, TPS changes, pauses, restarts, and failover (INV-GEN-3, INV-CHA-2, INV-G-4). Checkpoints carry RNG cursor positions so a restored shard continues the same draw sequence.
4. **Per-stream virtual clock:** `{virtual_epoch, speed_multiplier k (default 1.0), mode ∈ live|backfill, backfill_days}`, pinned at stream start (PIN-4). In live mode `virtual_now = v_i + k × (wall_now − w_i)` per run segment; the clock freezes while paused/stopped and rebases on resume. It stamps `occurred_at` and drives every business-time quantity — dwell times, lifecycle latencies, intensity curves, return windows ([../03-domain/event-model.md](../03-domain/event-model.md) §3 owns the full clock-domain reference table).
5. **No generator may read wall time.** `time.now` returns virtual time; hooks receive only `(rng, args, entity)`; wall-clock leakage is determinism poisoning (threat T-9) and is hunted by golden-replay CI.
6. **Backfill mode** advances the virtual clock as fast as generation allows over `[virtual_epoch, virtual_epoch + N days]`, materializing complete lifecycles with correct dwell and intensity shape as a bounded JSONL dataset (quota caps per PRD §7: 7 d/1M Free up to 90 d/20M Pro). A 30-day backfill visibly showing the diurnal/weekly shape is a Phase 8 exit criterion.
7. **Deterministic UUIDv7:** `event_id` timestamp bits encode `occurred_at` (simulated) milliseconds; random bits come from the seeded PRNG — ids reproduce with the sequence (ADR-0004, event-model §2.2.1).

The sub-seed namespaces, exhaustively:

| Namespace | Consumed by | What it makes reproducible |
|---|---|---|
| `values` | Attribute generators, `event_id` random bits | Every payload value and every event id |
| `transitions` | State-machine selection draws, dwell sampling | Every actor's path through every machine |
| `pools` | Entity-key hex digits, pool seeding, background-mutation scheduling | The seeded population and its drift schedule |
| `chaos` | Injection selection, delays, field mutations (ADR-0009) | Every injection — same config, same chaos, to the event |

Isolation is the point: Chaos consuming only `chaos` means toggling a mode never shifts a single canonical draw, so a chaos-on stream's ledger equals the chaos-off stream's output for the same seed.

## Alternatives considered

- **No virtual clock — wall-clock generation only.** Rejected: multi-day lifecycle latencies make every cross-funnel exercise infeasible in human time; backfill is impossible, abandoning the analytics-engineer persona (E7); and the PRD §4.2 latency contract would be untestable in CI at any realistic duration.
- **Record-and-replay reproducibility** (persist the generated stream; replay the recording for the next cohort). Rejected: storage-bound — the ledger retains 7 days, not semesters; a recording cannot be re-derived under a config tweak (lower F4, same seed — a routine instructor move); and it does nothing for failover, which needs to *continue* an in-flight sequence, not replay a finished one.
- **Best-effort determinism** (seed value generators only; accept timing and selection nondeterminism). Rejected: golden replay byte-identity fails, so the permanent CI anchor is gone; failover forks sequences (the ledger and a restored runner disagree); chaos injections stop reproducing, so answer keys cannot be regenerated and graded labs are unrepeatable.
- **Runtime-mutable speed multiplier.** Rejected: rebasing `k` mid-stream changes the wall realization of every in-flight dwell timer and pending simulated-time chaos delay (event-model §3.4), and the determinism unit would have to absorb the mutation history to stay replayable. Changing `k` is a new stream (PIN-4); the cost is honest and visible in the console.
- **A central sequence/timestamp service** for cross-shard global ordering instead of per-shard determinism. Rejected: a global ordering point is a throughput ceiling contradicting the 100k-TPS staircase (D15); per-shard `sequence_no` plus per-entity `entity_version` already provide every order the exercises need (event-model §2.2.2, §4.2).

## Consequences

### Positive

- Labs are reproducible and gradable across cohorts; golden-seed replay pins the entire generation + chaos pipeline in CI forever (INV-G-4).
- The kill-test exit criterion (Phase 5) is provable: a failed-over shard's output is byte-comparable against the expected sequence.
- One mechanism serves three personas: speed multiplier compresses lifecycles for streaming learners, backfill materializes history for batch/dbt learners, default 1× keeps wall and simulated domains coincident for beginners.

### Negative

- Determinism is an invasive discipline: every random draw in generation *and* chaos must route through namespaced sub-seeds; any wall-clock read or unseeded RNG anywhere in the data plane is a bug class requiring permanent golden-replay coverage and import-level lint.
- Deterministic UUIDv7 deviates from RFC 9562's wall-clock intent — documented deliberately so it never surprises an implementer (event-model §2.2.1).
- Pinning the multiplier and `virtual_epoch` means temporal experimentation requires new streams; accepted as the price of a closed determinism unit.

### Follow-ups

- [../04-engines/behavior-engine.md](../04-engines/behavior-engine.md) owns the sub-seed derivation tree, RNG cursor checkpoint format, and backfill execution; [../03-domain/event-model.md](../03-domain/event-model.md) §3 is the normative clock-domain table.
- Phase 4 ships seeded generation with byte-identical golden tests; Phase 8 ships the speed multiplier, backfill mode with quota caps, and the diurnal/weekly shape tests; Phase 9 extends golden replay to chaos injections (identical seed + config ⇒ identical injections).
