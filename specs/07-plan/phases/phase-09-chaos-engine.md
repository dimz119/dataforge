# Phase 9 — Chaos Engine + Instructor Answer Key

**Deliverable:** D18 (phase doc)

This phase ships the product's stated differentiator: all seven failure modes as a seeded, ordered, composable transform stage running post-ledger and pre-publish (ADR-0009), with every injection recorded before delivery and queryable by instructors through the answer-key API (ADR-0017). Business truth is never corrupted — the ledger stays clean and every delivered deviation maps to exactly one InjectionRecord — which is what turns chaos from a noise generator into a gradable teaching instrument. Mode mechanics, stage ordering, and the late-arrival buffer design are owned by [../../04-engines/chaos-engine.md](../../04-engines/chaos-engine.md); clock-domain rules for temporal parameters are frozen in [../../03-domain/event-model.md](../../03-domain/event-model.md) §3.4.

## Goal

> Ship the differentiator: all 7 failure modes, deterministic, gradable.

## Dependencies

- **Phase 8** — full funnel + CDC (chaos applies to CDC envelopes too, R-CDC-6) and the virtual clock (simulated-time chaos parameters realize as `wall_delay = simulated_delay / k`).
- **Phase 4** — ground-truth ledger (chaos input; INV-GEN-5 ordering: ledger write precedes chaos read).
- **Phase 3** — schema registry: drift mode injects only registered next-version fields (INV-REG-5; drift linkage DR-1..6 in [../../04-engines/schema-registry.md](../../04-engines/schema-registry.md) §11 — at this phase only v1 exists, so drift arms only after Phase 10 registers v2, and the CH-V07 arming check returns 422 `manifest-validation-failed` until then; the mode, menu mechanics, and tests ship now against test-registered fixtures).
- **Phase 7** — console (the `chaos` and `answer-key` tabs activate on the existing stream detail page).
- **Phase 6** — pause/resume checkpoints (the late-arrival buffer's lifecycle semantics, INV-CHA-5).

## Scope

1. **Pipeline-stage framework**: composable seeded stages in the normative order `missing → duplicates → corrupted_values → nulls → schema_drift → out_of_order → late_arriving`; each stage a pure transform consuming the `chaos` sub-seed.
2. **The seven modes** with per-mode rate + parameters (config schema owned by chaos-engine.md): duplicates (byte-identical copies), late-arriving (delay distribution in simulated time, persistent wall-clock re-publish buffer), missing (suppression), out-of-order (bounded simulated-time window shuffle), corrupted values (within-type mutations, e.g. `amount: "abc"`), nulls (payload fields only, never envelope), schema drift (registered next-version fields only, never CDC `before` images).
3. **Late-arrival buffer lifecycle** (the panel-gap design): entries `{event ref, due_at, state}` persist across pause and runner failover; `stop` applies `OnStopPolicy` (`discard` default / `flush`); outcomes recorded on injection records.
4. **Runtime toggling**: per-mode enable/rate/params live-mutable via the streams API (PIN-3), audit-logged (`streams.stream.chaos_policy_changed`); picked up next tick.
5. **Ground-truth recording**: InjectionRecord written before the affected instance is published or suppressed (INV-CHA-4); internal `_df` labels ride to the strip boundary, never delivered (INV-DEL-2).
6. **Answer-key API + console panel**: cursor-paginated injection queries (mode/time filters), per-mode counts, JSONL export; gated by workspace-admin role or `answer_key:read` scope; access audit-logged.
7. **Exercise presets** ([../../01-product/prd.md](../../01-product/prd.md) §5 catalog): "Dedup 101" (E1, `duplicates{rate:0.05}`), "Late data 30 min" (E2, `late_arriving{median:PT30M, rate:0.03}`), "Out-of-order" (E3, `out_of_order{window:PT60S, rate:0.10}`), "Drift day" (E5, `schema_drift`, armable post-Phase 10), "DLQ day" (E6, `corrupted_values{rate:0.02}+nulls{rate:0.02}`) — applied as bundles with a confirm diff in the console `PresetPicker`.
8. **Console**: `ChaosPanel` (7 mode cards, preset picker, `OnStopPolicySelect`, drift-disabled note) and `AnswerKeyPanel` per [../../02-architecture/frontend-architecture.md](../../02-architecture/frontend-architecture.md) §9.5.

## Non-goals

- **No v2/v3 schema registration or mid-stream upgrades** — Phase 10 (drift mechanics ship now; productive arming follows the registrations).
- **No new chaos modes beyond the seven** — the stage framework is the extension seam; new modes are post-MVP instances through it.
- **No grading workflows** (rubrics, submissions, scores) — the answer key exposes ground truth; grading happens in the instructor's tooling.
- **No chaos on the batch/backfill download path beyond what the stage framework gives for free** — backfill chaos uses the same transforms (event-model §3.4 backfill row); no special-case code.

## Tasks

- [ ] **P9-01** — Stage framework: ordered stage runner, per-stage sub-seed derivation, stage-order structural unit test; stages are pure functions over envelope batches.
- [ ] **P9-02** — Modes 1/2: `missing` + `duplicates` with InjectionRecords; statistical fixtures.
- [ ] **P9-03** — Modes 3/4: `corrupted_values` (within-type mutation table) + `nulls` (payload-only); field-level mutation records.
- [ ] **P9-04** — Mode 5: `schema_drift` — drift field menu from registry (DR-1), type-directed value synthesis from the `chaos` sub-seed (DR-2), CH-V07 arming check, never-into-`before` rule.
- [ ] **P9-05** — Mode 6: `out_of_order` — simulated-window displacement, displacement recorded.
- [ ] **P9-06** — Mode 7: `late_arriving` — persistent buffer (Postgres-backed schedule), `due_at = canonical emitted_at + simulated_delay / k`, re-emission worker, realized-delay recording.
- [ ] **P9-07** — Buffer lifecycle: pause-hold/resume-prompt-emit, `OnStopPolicy` discard/flush, lease-failover takeover of pending entries; CHD-8 fixtures.
- [ ] **P9-08** — ChaosPolicy API: live per-mode toggles within pinned bounds, validation (rate ≤ 0.5), audit entries; runner desired-state pickup.
- [ ] **P9-09** — Answer-key API: injection list/count endpoints, cursor pagination, mode/time filters, JSONL export, `answer_key:read` scope enforcement + TEN probes.
- [ ] **P9-10** — Presets: server-side preset catalog with expected-output templates; `PresetPicker` console UI.
- [ ] **P9-11** — Console: `ChaosPanel` + `AnswerKeyPanel` tabs; E2E `chaos-answer-key.spec.ts`.
- [ ] **P9-12** — Test assets: `SEED_GOLD_C` fixture (all-7-modes, 5k delivered events + injection projection); STAT-C batch-C transform harness; CHD-7 128-combination nightly matrix.

## Demo script

```bash
# Stream running at 50 TPS from Phase 8 demo; apply the Dedup 101 preset bundle
# (dedup_101, chaos-engine §8) as its expanded mode document:
curl -s -X PATCH localhost:8000/api/v1/streams/$SID/chaos \
  -H "X-API-Key: $KEY" \
  -d '{"duplicates":{"enabled":true,"rate":0.05,"params":{"copies":[{"count":1,"weight":1.0}],"spacing":{"mode":"adjacent"},"event_types":["*"]}}}'
# Consume 50k events and count repeated event_ids:
python demo/consume.py --stream $SID --count 50000 \
  | jq -s 'group_by(.event_id) | map(select(length>1)) | length'      # ≈ 2,500 (5% ± 1%)
# The answer key knows exactly which ones:
curl -s "localhost:8000/api/v1/streams/$SID/answer-key/injections?mode=duplicates" \
  -H "X-API-Key: $ADMIN_KEY" | jq '.data | length'        # equals the measured count
# Late-arrival lifecycle: enable late mode, pause with pending re-emissions, resume:
curl -s -X PATCH .../chaos \
  -d '{"late_arriving":{"enabled":true,"rate":0.03,"params":{"delay":{"family":"lognormal","median":"PT30M","p95":"PT2H"}}}}'
curl -s -X POST .../streams/$SID/pause -H "X-API-Key: $KEY"   # pending due_at entries held (INV-CHA-5)
curl -s -X POST .../streams/$SID/resume -H "X-API-Key: $KEY"  # held entries emit promptly; tail shows old occurred_at, new emitted_at
```

Console: open the stream's `chaos` tab, toggle modes live; open `answer-key` tab as admin, filter by mode, export the injection report (the instructor grading flow from PRD §2.2).

## Exit criteria

| # | Criterion | Proof ([../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md)) | Lane |
|---|---|---|---|
| 1 | Per-mode statistical accuracy: configured 5 % realizes 5 % ± 1 % over 50k events, for every mode; late events honor `occurred_at` < delivered `emitted_at` with realized simulated-delay median within ±15 % (and wall realization correct at k = 60) | STAT-C1..C7 | merge + gate run |
| 2 | Determinism: identical `(seed, chaos config)` ⇒ identical injections on the deterministic projection; GOLD-C byte-identity under injected wall clock | CHD-1/2/3 | PR (permanent) |
| 3 | All 2⁷ = 128 toggle combinations run crash-free with full reconciliation | CHD-7 matrix | nightly + gate run |
| 4 | Answer-key counts exactly match delivered chaos, to the event; ledger content hash unchanged by any chaos run | CHD-4/5 | PR |
| 5 | A paused stream resumes with pending late re-emissions intact; stop honors `OnStopPolicy`; failover hands pending entries to the new lease holder | CHD-8 | merge |
| 6 | Max-rate chaos (all modes at 0.5) never leaks across workspaces; answer key inaccessible with foreign or under-scoped credentials | TEN §7.5(P9) | PR (permanent) |
| 7 | Drift injections resolve only to registered next-version fields; envelope fields never nulled or corrupted | CHD-6 + STAT-C5/C6 extra assertions | PR |
| 8 | Console chaos flow works end-to-end: preset → repeated `event_id`s in tail → answer-key counts match API | E2E `chaos-answer-key.spec.ts` | nightly |
