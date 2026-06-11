# Phase 10 — Schema Evolution Exercises

**Deliverable:** D18 (phase doc)

This phase turns the schema registry — running since Phase 3 — into a teaching instrument: the documented e-commerce v2/v3 additive evolutions are registered, streams gain a scheduled mid-stream upgrade ("evolve to v2 at T+x" in simulated time), the registry becomes browsable in the console, and the two evolution exercises (E4 SCD2-via-CDC, E5 schema-drift day) become reproducible end-to-end. All mechanics were fixed in [../../04-engines/schema-registry.md](../../04-engines/schema-registry.md) (§9 the v1/v2/v3 trio, §10 pinning + upgrade schedule, §11 drift linkage); this phase implements them and closes the loop with Phase 9's `schema_drift` mode, which until now had no registered next version to draw from.

## Goal

> The registry becomes a teaching instrument: versioned schemas evolving mid-stream.

## Dependencies

- **Phase 3** — registry subjects/versions, `BACKWARD_ADDITIVE` enforcement, read API.
- **Phase 8** — full manifest 1.1.0 (the upgrade bindings `{"from": "actor.address.state"}` resolve against its emission contexts; REG-U005), CDC feed for E4.
- **Phase 9** — `schema_drift` mode and InjectionRecords (this phase arms drift productively and adds the upgrade↔drift menu rebuild, DR-4).
- **Phase 7** — console (registry browser routes activate; stream detail gains the upgrade schedule control).
- Specs: schema-registry.md (normative throughout), [../../03-domain/event-model.md](../../03-domain/event-model.md) §3.5 (the upgrade `at` is simulated time — the determinism-preserving choice).

## Scope

1. **Register v2/v3**: the documented additive evolutions for `ecommerce.order_placed` (v2 adds optional `shipping_state`; v3 adds optional `shipping_city` per schema-registry §9) via the explicit-registration flow (Flow 2), with stored `added_fields` diffs and emission bindings.
2. **Per-stream schema pinning surfaced**: `GET /api/v1/streams/{id}/schema-versions` returning `{effective, pending, applied}`; pin copied at start, effective map checkpoint-persisted (survives pause/failover, same guarantee class as INV-CHA-5).
3. **Scheduled mid-stream upgrade**: `POST/DELETE /api/v1/streams/{id}/schema-upgrades` with the full REG-U001..U007 validation catalog; runner cutover atomic between events, keyed on `occurred_at ≥ at` so it is replay-identical at any speed multiplier and consistent across shards; version skipping (1 → 3) applies the union of chains.
4. **Drift linkage completion**: menu rebuild at every upgrade application (DR-4); arming check CH-V07 now satisfiable; configurable ceiling (default `effective + 1`).
5. **Registry browser UI**: subjects table, version timeline, per-version `JsonViewer`, additive-only `SchemaDiff` (added fields in green — removals cannot exist, INV-REG-3); compat-violation 409s (`schema-incompatible` problem type with per-field violation list) surfaced in API and console.
6. **Exercise documentation**: E4 (SCD2 via CDC with dbt snapshot, graded against the ground-truth mutation log) and E5 (drift detection, then announced-upgrade adaptation) written into the PRD exercise catalog's lab docs with step-by-step consumer instructions.

## Non-goals

- **No Confluent Schema Registry deployment or mirroring** — subject naming has been Confluent-compatible since Phase 3 (INV-REG-1); mirroring executes with the Phase 12 external-Kafka channel (schema-registry §13).
- **No CDC subject upgrades** — REG-U006 rejects `cdc.*` upgrade scheduling by design (synthesized row-image fields would violate INV-GEN-6); CDC evolution arrives only via new manifest versions.
- **No non-additive compatibility modes** — `BACKWARD_ADDITIVE` is the only mode in MVP.
- **No manifest changes** — v2/v3 are registry-side evolutions of payload schemas; manifest 1.1.0 is untouched (the upgrade bindings live in the registration, not the manifest).

## Tasks

- [ ] **P10-01** — Flow 2 registration endpoint: explicit version registration with compat check, `added_fields` diff storage, binding validation against emitting manifest contexts (REG-U005 inputs); 409 `schema-incompatible` with per-field violation list.
- [ ] **P10-02** — v2/v3 fixtures + seed command: register the schema-registry §9.3/§9.4 documents for `ecommerce.order_placed`; stored diffs verified (`shipping_state`, then `shipping_city`).
- [ ] **P10-03** — Stream schema pin: pin copy at start, effective-version map in checkpoint format, `GET /api/v1/streams/{id}/schema-versions` returning `{effective, pending, applied}`.
- [ ] **P10-04** — Upgrade schedule API: create/cancel endpoints, REG-U001..U007 validation surfacing RFC 9457 `409` `conflict` problems with the `errors[]` extension (api-spec §4.8.4 / schema-registry §10.3), audit entries (`schema_upgrade_scheduled`/`_cancelled`).
- [ ] **P10-05** — Runner cutover core: pre-warmed compiled target schemas via desired state, resolver extension with chain `added_fields` bindings, atomic between-events switch on `occurred_at ≥ at`; version skipping (1 → 3) applies the union of chains.
- [ ] **P10-06** — Cutover bookkeeping: `applied` marking with `applied_at_wall` + per-shard `applied_sequence_no`, audit `schema_upgrade_applied`, `pending → applied` transition surfaced in `GET /schema-versions`.
- [ ] **P10-07** — Lifecycle, control plane: upgrade vs pause (frozen clock cannot fire), stop/restart (pending schedules survive in checkpoint + desired state), failover (fires on first post-restore tick).
- [ ] **P10-08** — Lifecycle, data plane: a day-10 upgrade inside a 30-day backfill cuts over at the simulated boundary; late re-emissions keep their original `schema_ref` (v1 stragglers after cutover).
- [ ] **P10-09** — Drift menu rebuild on upgrade application (DR-4); ceiling config (default `effective + 1`); CH-V07 end-to-end test (arming fails before v2 exists, succeeds after).
- [ ] **P10-10** — Console registry browser: `RegistryBrowserPage` + `SubjectDetailPage` + additive-only `SchemaDiff`; compat-violation 409s surfaced inline.
- [ ] **P10-11** — Console upgrade control: scheduled-upgrade form + pending/applied state on stream detail; E2E `registry.spec.ts`.
- [ ] **P10-12** — Exercise docs: E4 SCD2 lab (dbt snapshot recipe + answer-key grading steps), E5 drift-then-upgrade lab; both runnable against `SEED_E2E = 4242`.

## Demo script

```bash
# Live stream from Phase 8/9 demos, emitting order_placed v1. Schedule the upgrade:
curl -s -X POST localhost:8000/api/v1/streams/$SID/schema-upgrades \
  -H "X-API-Key: $KEY" \
  -d '{"subject":"ecommerce.order_placed","target_version":2,"at":"2026-06-12T00:00:00.000000Z"}'
# Watch the cutover in the tail (60× stream: simulated midnight arrives quickly):
curl -s "localhost:8000/api/v1/streams/$SID/events?event_type=order_placed" -H "X-API-Key: $KEY" \
  | jq '.data[] | {seq: .sequence_no, v: .schema_ref.version, state: .payload.shipping_state}'
#   → v1 rows (no shipping_state), then v2 rows (+shipping_state), no restart, stream never stops
curl -s localhost:8000/api/v1/streams/$SID/schema-versions -H "X-API-Key: $KEY" \
  | jq '{effective, applied: [.applied[].subject]}'
# Consumer-side resolution of both versions:
curl -s localhost:8000/api/v1/schemas/subjects/ecommerce.order_placed/versions/1/schema | jq .properties
curl -s localhost:8000/api/v1/schemas/subjects/ecommerce.order_placed/versions/2/schema | jq .properties
# Compat enforcement demo — a removal is rejected:
curl -s -X POST .../subjects/ecommerce.order_placed/versions -d @fixtures/v4-removes-field.json
#   → 409 schema-incompatible, violation list names the removed field
# E4: run the SCD2 dbt snapshot against the cdc.users feed; diff against the answer-key mutation log → byte-equal
```

Console: registry browser shows the subject's version timeline; the v1→v2 diff renders `shipping_state` as the single green addition; the stream detail shows pending → applied upgrade state.

## Exit criteria

| # | Criterion | Proof ([../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md)) | Lane |
|---|---|---|---|
| 1 | Live stream emits v1, upgrades to v2 at the scheduled simulated time **without restart**; consumers resolve both versions from the registry; v1 late stragglers after cutover keep their original `schema_ref` | OPS-13 + CON registry tests | merge + gate run |
| 2 | Cutover is deterministic and lifecycle-safe: same pin + seed + schedule reproduces the same cutover `sequence_no`; pause/stop/failover/backfill interactions per schema-registry §10.4 | GOLD variant + OPS-13 lifecycle cases | merge |
| 3 | Drift-injected fields always resolve to a registered version above the effective version; upgrade application rebuilds the drift menu (post-upgrade drift uses v3 only) | CHD-6 + DR-6 property test | PR (permanent) |
| 4 | REG-U001..U007 each rejected with the documented problem code; compat violations surface as 409 with per-field details in API and console | UNIT validation fixtures + CON | PR |
| 5 | SCD2 exercise reproducible end-to-end: dbt-snapshot output byte-equal to the table derived from the answer key's ground-truth mutation log | CDC-8 | nightly + gate run |
| 6 | Registry browser works: subject list, version history, additive diff, compat-error surfacing | E2E `registry.spec.ts` | nightly |

With this phase complete, every MVP teaching surface exists — clean realism (Phase 8), gradable chaos (Phase 9), announced evolution (Phase 10) — and the upgrade-vs-drift distinction of schema-registry.md §10.6 is demonstrable on one live stream. Phase 11 hardens the platform; it adds no new learner-facing capability.
