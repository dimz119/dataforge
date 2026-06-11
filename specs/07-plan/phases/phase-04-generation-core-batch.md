# Phase 4 — Generation Core + Batch Datasets

**Deliverable:** D18 (phase doc)

Phase 4 makes the manifest move: the generic behavior engine interprets the pinned subset manifest over seeded entity pools, writes referentially valid canonical events to the ground-truth ledger, and exposes batch generation with JSONL download. Everything is validated end-to-end **without streaming infrastructure** — determinism and referential integrity are proven on bounded batches before a single event touches Kafka — and the batch endpoint delivers the first learner value (datasets for dbt/DuckDB, exercise E7 v1).

## Goal

> First real referentially valid, deterministic events from the manifest — validated end-to-end without streaming infrastructure; first learner value (bulk datasets for DBT/DuckDB).

## Dependencies

| Dependency | Role |
|---|---|
| Phase 3 complete | Validated manifests, registered v1 schemas, envelope library, scenario instances + pinning model |
| [../../04-engines/behavior-engine.md](../../04-engines/behavior-engine.md) | The spec this phase implements: ManifestIR, interpreter semantics, pool mechanics, sub-seeds, injectable clocks, checkpoint format |
| [../../04-engines/scenario-plugin-architecture.md](../../04-engines/scenario-plugin-architecture.md) §6, §8.4 | Transition-selection/remainder/guard/dwell rules; layer-3 dry-run contract (MAN-D601–605) |
| [../../03-domain/event-model.md](../../03-domain/event-model.md) | Envelope stamping, canonical serialization, deterministic UUIDv7, clock domains |
| [../../03-domain/database-schema.md](../../03-domain/database-schema.md) | Ledger time-partitioning DDL, entity-pool snapshot tables |
| [../../01-product/prd.md](../../01-product/prd.md) §4, §7 | Funnel/latency defaults under test; backfill quota caps |
| ADR-0007 (state machines over pools), ADR-0008 (seeds + virtual clock), ADR-0009 (ledger stage), ADR-0012 (state-first mutations) | Structural decisions implemented here |

## Scope

- **Entity-pool seeding from the manifest:** `seeding.catalogs` sizes (configurable within `[min, max]`, Σ ≤ 250,000 per B-08); Redis hot state + periodic Postgres snapshots; entity keys `{key_prefix}_{16 hex}` drawn from the `pools` sub-seed; automatic `created_at`/`updated_at` maintenance; `entity_version` incremented on every mutation from day one (so Phase 8 CDC images are derivable without rework).
- **Behavior engine v1:** ManifestIR compilation (cumulative probability tables, compiled guards, generator closures bound to sub-seeds, process-local LRU cache per scenario-plugin §10.3); state-machine interpreter implementing §6.2 exactly (sum rule, remainder policies, guard fall-through without re-draw, dwell sampling, timeout edges, self-transitions); the 41-generator vocabulary + `derived.expr` evaluator; sub-seed derivation `HMAC(seed, namespace)` for `values`/`transitions`/`pools`/`chaos`; virtual clock with **injectable wall clock** (the GOLD harness requirement, testing-strategy §6); unpaced backfill-style execution for batches.
- **Ground-truth ledger:** append-only, time-partitioned Postgres writes of the full internal envelope (`_df.canonical: true`, canonical serialization S-2); gapless `sequence_no` per (stream, shard); 7-day default retention (drop/backup jobs refined in Phase 11 — partitions and the manual drop command exist now).
- **Batch generation:** `POST /api/v1/batches` (scenario instance + seed + event-count or simulated-days bound); small requests synchronous, large requests on the Celery `exports` queue (backend-architecture §queue table) with status polling and gzipped JSONL download; caps per the PRD §7 backfill quota rows enforced at command time.
- **Validator layer 3** (dry run, MAN-D601–605 + W-D610–612) as a Celery `validation` job executing the real runtime in sandboxed bounded mode (seed `424242424242`, 30 s / 256 MiB / 50k events); builtin manifests re-validated through L3 in CI (closes the Phase 3 sequencing window).
- **Golden + property suites land permanently:** GOLD-A (subset manifest, 1k events, deterministic injected wall clock); PROP-RI-1…8 over 100k-event PR batches and the 1M-event nightly/gate profile. GOLD-B/C extend the harness when their features land (Phases 8–9).
- **Exercise E7 v1 documentation:** load the JSONL into DuckDB, build staging models — the OPS-11 script (full diurnal/funnel realism arrives Phase 8).

## Non-goals

| Deferred | Lands in |
|---|---|
| Streaming, runners, leases, Kafka production (topics stay idle) | Phase 5 |
| Pause/resume + periodic checkpointing (the checkpoint *format* is implemented for batch finalization; lease-driven lifecycles are next) | Phases 5–6 |
| CDC event emission (mutations are versioned now; `cdc.*` events and snapshot `r` rows emit later) | Phase 8 |
| Intensity curves, speed multiplier, long-horizon backfill realism (batches run with flat intensity; curve evaluation ships with realism work) | Phase 8 |
| Chaos transforms (the `chaos` sub-seed namespace is reserved and derived now, consumed later) | Phase 9 |
| Full 8-entity manifest (batches run the subset) | Phase 8 |

## Tasks

- [ ] Pool store: Redis hot-state layout + Postgres snapshot tables + seeding from `seeding.catalogs` (B-08 enforcement)
- [ ] Deterministic key generation `{prefix}_{16 hex}` from the `pools` sub-seed; `entity_version` + timestamp maintenance
- [ ] Sub-seed derivation module with documented HMAC test vectors (`values`/`transitions`/`pools`/`chaos`)
- [ ] ManifestIR compiler: probability tables, compiled guards, generator closures, LRU cache keyed `slug:version`
- [ ] Generator vocabulary: identity/person/address + commerce/internet/text groups with per-param validation
- [ ] Generator vocabulary: numeric/choice/time + `template`/`ref.fk`/`ref.attr`/`derived.expr` (closed grammar §4.5)
- [ ] Interpreter core: selection draw, remainder rule, guard fall-through, dwell sampling, timeout edges, session timeout absorption
- [ ] Effects executor: `create`/`update`/`adjust`/`delete`/`remember` in declaration order; precondition guards over relationship indexes (INV-GEN-1/2)
- [ ] Virtual clock + injectable wall clock; deterministic UUIDv7 wiring into envelope emission
- [ ] Ledger writer: time-partitioned append-only inserts, canonical serialization, gapless `sequence_no`
- [ ] Batch API: sync path, Celery `exports` path, status polling, gzipped JSONL download, quota caps
- [ ] Validator L3 dry run as Celery job (MAN-D601–605, warnings, `est_eps_per_shard`, `mean_events_per_session` persisted)
- [ ] CI job re-validating all builtin manifests through L3 (GUARD)
- [ ] GOLD-A fixture + replay harness with deterministic wall clock; `make golden-regen` local script + `golden-rebaseline` label convention
- [ ] PROP-RI-1…8 batch property suite (100k PR profile, 1M nightly/gate profile)
- [ ] E7 exercise doc + OPS-11 DuckDB assertion script

## Demo script

1. `docker compose -f infra/compose/compose.yaml up -d --wait`; obtain `$ACCESS`/`$WS` via `infra/scripts/demo-phase02.sh`.
2. Create an instance: `INST=$(curl -s -X POST localhost:8000/api/v1/scenario-instances -H "Authorization: Bearer $ACCESS" -d '{"workspace_id":"'$WS'","scenario_slug":"ecommerce","manifest_version":"1.0.0"}' | jq -r .scenario_instance_id)`.
3. Small sync batch: `curl -s -X POST localhost:8000/api/v1/batches -H "Authorization: Bearer $ACCESS" -d '{"scenario_instance_id":"'$INST'","seed":4242,"max_events":5000}' -o batch1.jsonl.gz` → 5,000-event JSONL.
4. Determinism: repeat step 3 into `batch2.jsonl.gz`; `cmp <(gunzip -c batch1.jsonl.gz) <(gunzip -c batch2.jsonl.gz)` → identical bytes.
5. Inspect an event: `gunzip -c batch1.jsonl.gz | head -1 | jq` — all 20 envelope keys, `schema_ref.version: 1`, `shard_id: 0`, RFC 3339 timestamps with 6 fractional digits.
6. Referential spot-check: `gunzip -c batch1.jsonl.gz | jq -r 'select(.event_type=="order_placed") | .payload.user_id' | head -3` — each id appears in an earlier `user_registered`/pool-seeded entity event position (`PROP-RI` does this exhaustively).
7. Large async batch: `POST /api/v1/batches` with `max_events: 100000` → `202` + `batch_id`; poll `GET /api/v1/batches/$BATCH` until `completed`; download.
8. DuckDB exercise (OPS-11): `duckdb demo.db -c "CREATE TABLE events AS SELECT * FROM read_json_auto('batch100k.jsonl');"` then the three documented E7 queries — row count = 100,000; orders→users FK join matches 100%; daily-revenue rows present.
9. Gate suites: `pytest -m golden -q` (GOLD-A byte-identity) and `pytest -m property -q` (PR profile); trigger the 1M nightly profile for the attended gate run.
10. L3 demo: POST a manifest passing L1/L2 but containing a near-absorbing `stay` loop → validation report (polled) shows `MAN-D602` after the Celery dry run.

## Exit criteria

Binding text with measurable assertions; proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 4 rows).

| # | Binding criterion | Measurable assertion | Proving suite (lane) |
|---|---|---|---|
| 1 | "Golden tests: fixed seed reproduces byte-identical batches" | GOLD-A replays byte-identically under the injected wall clock (full envelope, wall fields included); first divergent line/field reported on mismatch | GOLD-A (PR, permanent; GOLD-B/D join in Phases 8/6) |
| 2 | "invariant tests prove referential validity over a 1M-event batch (no payment without order, no event references a nonexistent entity)" | PROP-RI-1…8 pass over a 1,000,000-event batch at the pinned seed: entity-reference resolution, payment⇒order, gapless `sequence_no`, monotone `occurred_at`, causality chain resolution, `schema_ref` resolution, 20-key envelope | PROP-RI 1M profile (nightly + attended gate run) |
| 3 | "a 100k-event dataset loads into DuckDB/dbt per a documented exercise" | OPS-11 script: 100,000 rows loaded, 100% orders→users join match, daily-revenue query returns rows — exactly the published E7 commands | OPS-11 (merge) |
| 4 | Builtins pass validator layer 3 (sequencing close-out from Phase 3) | The L3 re-validation CI job runs every builtin manifest through the dry run; all pass with `est_eps_per_shard ≥ 1,000` | GUARD: L3 re-validation job (merge) |
