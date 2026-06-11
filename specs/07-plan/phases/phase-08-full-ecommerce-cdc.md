# Phase 8 — Full E-Commerce, CDC, and Realism

**Deliverable:** D18 (phase doc)

This phase delivers scenario depth: the full 8-entity e-commerce simulation (~20 event types), first-class CDC emission, and the concrete realism contract of [../../01-product/prd.md](../../01-product/prd.md) §4 — intensity curves, virtual-clock speed multiplier, and backfill. The full manifest was written and paper-validated at Phase 0 ([../../04-engines/scenarios/ecommerce.md](../../04-engines/scenarios/ecommerce.md)) precisely so this phase is *registration and engine capability*, not DSL invention: the runtime stays generic (ADR-0003), CDC and business events derive from the same entity-pool mutations (ADR-0012, state-first generation), and the CDC envelope shape has been frozen since Phase 0 ([../../03-domain/event-model.md](../../03-domain/event-model.md) §4).

## Goal

> Scenario depth: the 8-entity business simulation, first-class CDC, and concrete realism (curves, virtual clock, backfill).

## Dependencies

- **Phase 4** — behavior engine v1, entity pools, ground-truth ledger, batch generation + JSONL download (backfill extends it).
- **Phase 5/6** — streaming runtime, REST/WS delivery, pause/resume checkpoints (CDC and curves must survive all lifecycle transitions).
- **Phase 3** — manifest validator and registry; publishing manifest `1.1.0` derives all v1 schemas including `ecommerce.cdc.{entity}` subjects.
- **Phase 7** — console; this phase unlocks its virtual-clock controls and adds CDC affordances ([../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) §13).
- Specs: [../../04-engines/behavior-engine.md](../../04-engines/behavior-engine.md) (curves, virtual clock, backfill mechanics), [../../04-engines/scenario-plugin-architecture.md](../../04-engines/scenario-plugin-architecture.md) §7 (CDC config), event-model §3–4 (clock domains, CDC rules R-CDC-1..7).

## Scope

1. **Full manifest registration**: publish `ecommerce 1.1.0` (8 entities — Users, Products, Orders, Payments, Refunds, Inventory, Reviews, Shipments; ~20 event types) as builtin data; the Phase 3–7 subset (`1.0.0`) remains published for existing instances (INV-CAT-1/5).
2. **Generalized preconditions**: manifest-declared guards beyond the subset's needs — relationship-existence guards (the refund gate: delivered or lost shipment), attribute comparisons, return-window checks against the virtual clock (scenario-plugin §6.3). Zero e-commerce logic in Python remains a permanent GUARD gate.
3. **CDC emission** per ADR-0012: `op` ∈ `c`/`u`/`d` with full before/after images from pool mutations; snapshot `r` rows once per CDC-enabled seeded entity at stream head; background mutations (0.5 %/actor/day address drift) as chain roots; `source.entity_version` gapless per entity.
4. **Per-entity CDC filtering on consumption** (R-CDC-7): `event_type = "cdc.{entity}"` + `entity_refs` matching, identical semantics on REST and WS.
5. **Intensity curves**: diurnal/weekly multipliers renormalized to mean 1.0, evaluated on the simulated local clock (instance timezone); PRD §4.3 defaults in the manifest.
6. **Virtual clock**: `speed_multiplier` (1–1000) honored across dwell times, lifecycle latencies, curves, and chaos-parameter realization rules already frozen in event-model §3.2/§3.4.
7. **Backfill mode**: `mode: backfill, backfill_days: N` generating complete lifecycles unpaced, JSONL download headed by the CDC snapshot block, quota-capped per PRD §7 (Free 7 d/1M, Classroom 30 d/5M, Pro 90 d/20M).
8. **Console deltas**: virtual-clock section unlocks multipliers and backfill on stream create; CDC `op` chips and per-entity filter in the live tail.

## Non-goals

- **No chaos modes** — Phase 9 (instance chaos *defaults* are stored from Phase 7; nothing executes them yet).
- **No v2/v3 schemas, no mid-stream upgrades** — Phase 10; this phase registers v1 subjects only.
- **No runner sharding** — streams stay single-shard (`shard_id = 0`) until Phase 11.
- **No new delivery channels**; CDC filtering is a query capability on existing channels, not a new sink.
- **No additional scenarios** — the seam is proven by the reference manifest; more instances through it are post-MVP.

## Tasks

- [ ] **P8-01** — Publish full manifest `ecommerce 1.1.0`; `sync_builtin_scenarios` registers it and derives all v1 schemas (business + `cdc.*` subjects); L3 dry-run budget passes.
- [ ] **P8-02** — Engine: generalized precondition guards (relationship existence, attribute comparison, virtual-clock window checks) with guard fall-through to remainder per scenario-plugin §6.2 rule 3.
- [ ] **P8-03** — Engine: post-session lifecycle machines (payment → shipment → delivery → review/refund arcs) with the L1–L8 dwell distributions and hard-bound fallback events.
- [ ] **P8-04** — CDC core: pool-mutation hooks emit `c`/`u`/`d` with image chaining and `entity_version`; R-CDC-2 adjacency (consecutive `sequence_no`, shared `occurred_at`/`correlation_id`).
- [ ] **P8-05** — CDC snapshots + background mutations: `r` rows at stream head (`occurred_at = virtual_epoch`), manifest-declared attribute drift as chain-root CDC (R-CDC-3).
- [ ] **P8-06** — Per-entity CDC filter parameter on REST events endpoint and WS subscribe frame; identical matching semantics, contract-tested.
- [ ] **P8-07** — Intensity curves: renormalization, simulated-local-hour evaluation, session-arrival modulation; property test that curve shape never changes average TPS.
- [ ] **P8-08** — Virtual clock: speed-multiplier segment arithmetic, pause-freeze/resume-rebase, checkpoint persistence of clock position (extends Phase 6 checkpoints).
- [ ] **P8-09** — Backfill mode: unpaced generation over `[virtual_epoch, +N days]`, Celery-backed for large jobs, JSONL download with snapshot head; quota caps enforced at request time.
- [ ] **P8-10** — Console: unlock virtual-clock controls; CDC `op` chips + entity filter in `LiveTail`; E7 exercise doc (DuckDB/dbt) updated for full funnel.
- [ ] **P8-11** — Test assets: `SEED_GOLD_B` golden fixture (full manifest + CDC, 10k events); statistical batches A/B wired (`SEED_STAT`); 1M-event nightly profile for PROP-RI and CDC suites.

## Demo script

```bash
docker compose up -d --wait
# 1. Create instance of ecommerce@1.1.0 with defaults, CDC on for users/products/inventory (console or API)
# 2. Start a 60× stream — 2.5-simulated-day shipping compresses to ~1 wall-hour
curl -s -X POST localhost:8000/api/v1/streams \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"scenario_instance_id":"'$INSTANCE'","seed":4242,"target_tps":50,
       "virtual_clock":{"speed_multiplier":60,"mode":"live"}}'
# 3. Watch the funnel and CDC interleave; filter to one entity's CDC feed
curl -s "localhost:8000/api/v1/streams/$SID/events?entity_type=users&event_type=cdc.users" \
  -H "X-API-Key: $KEY" | jq '.data[].payload | {op, before: .before.address.city, after: .after.address.city}'
# 4. Backfill: 30 simulated days as a dataset
curl -s -X POST localhost:8000/api/v1/streams -H "X-API-Key: $KEY" \
  -d '{"scenario_instance_id":"'$INSTANCE'","seed":4242,"virtual_clock":{"mode":"backfill","backfill_days":30}}'
#    ... poll job, download events.jsonl, then:
duckdb -c "SELECT date_trunc('hour', occurred_at) h, count(*) FROM read_json_auto('events.jsonl')
           WHERE event_type='session_started' GROUP BY 1 ORDER BY 1;"   # diurnal + weekend shape visible
```

In the console: tail shows `cdc.users` rows with `u` chips; expanding one shows before/after address images; the first events of the stream are the `r` snapshot block.

## Exit criteria

| # | Criterion | Proof ([../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md)) | Lane |
|---|---|---|---|
| 1 | 1M-event soak with **zero** integrity violations: no refund without delivered/lost shipment, no payment without order, inventory never negative, every reference resolves | PROP-RI-1..8, 1M profile, full manifest | nightly + gate run |
| 2 | CDC consistency: no `u`/`d` before `c`/`r`; before-image chaining gapless `entity_version`; business/CDC adjacency; order/inventory reconciliation | CDC-1..7 | PR subset + nightly full |
| 3 | 30-simulated-day backfill shows diurnal/weekly shape: bucket shares within ±10 %/±5 % relative, peak-to-trough in [5.4, 6.6], 168-hour profile Pearson r ≥ 0.98 | STAT-SHAPE-1/2 (batch B) | nightly + gate run |
| 4 | Realized conversion rates within PRD tolerance at n ≥ 10k sessions: full funnel catalog incl. order-per-session ∈ [14 %, 19 %] | STAT-F1..F12 (50k-session batch A) | merge + gate run |
| 5 | Lifecycle latencies: realized medians within ±15 %, p95 within ±25 %, hard bounds produce fallback events | STAT-L1..L8 | merge |
| 6 | Determinism holds with CDC + curves: fixed seed reproduces byte-identical 10k-event batch | GOLD-B | PR (permanent) |
| 7 | Curves/clock survive lifecycle: pause freezes virtual time, resume rebases, restart continues (no shape discontinuity) | GOLD-D + OPS-4 + unit clock tests | merge |
| 8 | Per-entity CDC filtering returns identical event sets on REST and WS | XCH variant + CON | merge |
