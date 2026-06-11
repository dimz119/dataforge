# Phase 0 — Design Specs

**Deliverable:** D18 (phase doc); the phase itself produces deliverables D1–D20

Phase 0 is the only phase that produces no code: it authors the complete `specs/` tree — all twenty design deliverables, seventeen ADRs, and thirteen phase docs — and locks the one-way-door contracts whose retrofit cost dominates everything else. Its gate is the design **approval gate** ([README.md](README.md) §3): after the specs are committed, work stops for user review, and no application code exists before approval.

## Goal

> Author and approve all 20 deliverables plus the phase breakdown; lock the one-way-door contracts before any code.

## Dependencies

| Dependency | Role |
|---|---|
| None (phases) | The repo is empty and greenfield; Phase 0 is the root of the dependency graph |
| Requirements digest + panel-approved decision set | The binding inputs; decisions are recorded as ADRs and are not relitigated in the docs |
| [../../adr/README.md](../../adr/README.md) | ADR index, status legend, and template the seventeen ADRs follow |

Every later phase depends on this one: each phase doc's Dependencies table names the specs Phase 0 produced as its governing contracts.

## Scope

- **The full `specs/` tree:** `specs/README.md` index (explicit D1–D20 → file map, reading order, status table, glossary pointer), 22 deliverable docs across `01-product/` … `07-plan/`, 17 ADRs under `adr/`, and 14 files under `07-plan/phases/` (this README + 13 phase docs).
- **Freeze the five core contracts:**
  1. Canonical event envelope `1.0` — all 20 fields, Debezium-shaped CDC sub-envelope, clock-domain rules under the virtual clock, `_df` strip boundary ([../../03-domain/event-model.md](../../03-domain/event-model.md), ADR-0004/0012).
  2. Manifest JSON Schema v0 + validation rule catalog (MAN-S/V/D codes, bounds B-01…B-17) with the **full 8-entity e-commerce manifest** as the worked example and DSL expressiveness forcing function ([../../04-engines/scenario-plugin-architecture.md](../../04-engines/scenario-plugin-architecture.md), [../../04-engines/scenarios/ecommerce.md](../../04-engines/scenarios/ecommerce.md), ADR-0003).
  3. `DeliveryChannel` sink interface — `deliver(batch)`, cursor/ack semantics, backpressure signal — including contract-level detail for the future Kafka/webhook/S3-Iceberg sinks ([../../04-engines/delivery-channels.md](../../04-engines/delivery-channels.md), ADR-0005).
  4. Registry subject naming `{scenario_slug}.{event_type}`, Confluent-compatible ([../../04-engines/schema-registry.md](../../04-engines/schema-registry.md), ADR-0010).
  5. Tenancy model: shared schema, non-null `workspace_id` everywhere, scoped managers + CI guard + RLS ([../../06-quality/security-architecture.md](../../06-quality/security-architecture.md), ADR-0002).
- **Timeboxing:** non-blocking sections carry "Refined in Phase N" markers stating what exists today; only ADR-0002/0003/0004/0005/0009/0010 are review-blocking — the documented anti-stall rule for spec phases.
- **Phase breakdown:** each of the 13 phase docs follows the [README.md](README.md) §2 template with goal, dependencies, scope, non-goals, tasks, demo script, exit criteria.

## Non-goals

| Deferred | Lands in |
|---|---|
| Any application code, CI pipeline, compose stack, or deployment | Phase 1 |
| Manifest validator *implementation* (the JSON Schema document and rule catalog are specced now) | Phase 3 (L1+L2), Phase 4 (L3 dry run) |
| Registering any scenario or schema row (paper contracts only) | Phase 3 |
| Machine validation of the e-commerce manifest (inspection-validated now) | Phase 3 CI; Phase 8 registers the full manifest |
| Review sign-off on ADR-0001/0006–0008/0011–0017 (Accepted by authorship, reviewable async) | Rolling, before each ADR's first implementing phase |
| The AI prompt→manifest generation service (its slot-in contract is specced in scenario-plugin §12) | Post-MVP (Phase 12+) |

## Tasks

- [ ] `specs/README.md`: D1–D20 → file map, reading order, doc status table, glossary pointer
- [ ] `01-product/prd.md` (D1): personas, JTBD, core flow, concrete realism criteria, exercise catalog, quotas, metrics, NFR routing
- [ ] `02-architecture/system-architecture.md` (D2) + `backend-architecture.md` (D12) + `frontend-architecture.md` (D11)
- [ ] `02-architecture/deployment-architecture.md` (D13) + `scaling-strategy.md` (D15, with the 1→100k TPS capacity arithmetic) + `observability.md` (SLO definitions)
- [ ] `03-domain/domain-model.md` (D3, terminology authority + `INV-*` catalog) + `database-schema.md` (D4, DDL + RLS)
- [ ] `03-domain/event-model.md` (D5): envelope freeze, CDC shape, clock-domain rules, per-channel guarantees
- [ ] `04-engines/scenario-plugin-architecture.md` (D6): manifest schema v0, validation pipeline, pinning, AI slot-in, threat model
- [ ] `04-engines/behavior-engine.md` (D7) + `chaos-engine.md` (D8, incl. late-buffer lifecycle semantics) + `schema-registry.md` (D9)
- [ ] `04-engines/delivery-channels.md` (sink contract + REST/WS specs + future-channel contracts)
- [ ] `04-engines/scenarios/ecommerce.md`: full 8-entity, ~20-event-type reference manifest worked example
- [ ] `05-interfaces/api-specification.md` (D10): `/api/v1` catalog, cursor pagination, RFC 9457 errors, WS subprotocol
- [ ] `06-quality/security-architecture.md` (D14) + `testing-strategy.md` (D16, suite taxonomy + §14 gate table)
- [ ] `07-plan/incremental-roadmap.md` (D18) + `mvp-vs-future.md` (D20) + `project-folder-structure.md` (D19)
- [ ] `07-plan/phases/`: this README + phase-00 … phase-12 docs per the §2 template
- [ ] `adr/`: README + ADR-0001…0017; set ADR-0002/0003/0004/0005/0009/0010 to **Accepted** with review
- [ ] Cross-doc consistency pass: §6 terminology of [../../03-domain/domain-model.md](../../03-domain/domain-model.md) used exactly, every `INV-*`/`MAN-*`/`SEC-*` citation resolves, every relative link resolves, zero TODO/TBD markers
- [ ] Commit the tree and stop for the user approval gate

## Demo script

Phase 0 precedes CI, so the demo is a scripted documentation review executed at the repo root:

1. `find specs -name '*.md' | sort` — compare against the tree declared in `specs/README.md`; no file missing, no stray file.
2. Verify the deliverable map: open `specs/README.md`, confirm 20 rows D1–D20 each linking a file with a status; then check links mechanically:
   `grep -oE '\((\.\/)?[0-9a-z/_.-]+\.md\)' specs/README.md | tr -d '()' | while read f; do test -f "specs/$f" || echo "MISSING $f"; done` — prints nothing.
3. ADR gate: `grep -H '^.*Status' specs/adr/adr-0002* specs/adr/adr-0003* specs/adr/adr-0004* specs/adr/adr-0005* specs/adr/adr-0009* specs/adr/adr-0010*` — all six show `Accepted`; `grep -rLi 'status' specs/adr/adr-*.md` — empty (no ADR lacks a status).
4. Placeholder scan: `grep -rnE 'TODO|TBD|to be defined' specs/` — zero hits ("Refined in Phase N" is the only deferral marker).
5. Manifest inspection-validation: open [../../04-engines/scenarios/ecommerce.md](../../04-engines/scenarios/ecommerce.md) and walk it against the v0 schema in scenario-plugin §9.1 — required sections present; exactly one `session` machine binding `users`; per-state probability sums ≤ 1.0 and matching PRD §4.1 defaults; all referenced entities/relationships/event types declared; bounds B-01…B-17 satisfied; zero `hook` generators; refund transitions guarded on delivered/lost shipments. Optional mechanical assist: extract the YAML and schema and run `check-jsonschema --schemafile manifest-v0.schema.json ecommerce.yaml`.
6. Phase-doc completeness: `grep -L '## Demo script' specs/07-plan/phases/phase-*.md; grep -L '## Exit criteria' specs/07-plan/phases/phase-*.md` — both empty.
7. Confirm the repo contains no application code: `find . -name '*.py' -o -name '*.ts' -o -name '*.tsx' | grep -v specs` — empty.

## Exit criteria

Binding text, each row expanded with its measurable assertion. Phase 0 precedes CI, so the proving mechanism is reviewer execution of the demo script above; from Phase 1 onward, [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 takes over as the gate mechanism.

| # | Binding criterion | Measurable assertion | Proof |
|---|---|---|---|
| 1 | "specs/README.md maps every deliverable D1–D20 to a file with status" | 20 rows present; every linked file exists (demo steps 1–2) | Reviewer, demo steps 1–2 |
| 2 | "the six one-way-door ADRs are reviewed and Accepted (none TBD)" | ADR-0002/0003/0004/0005/0009/0010 carry `Status: Accepted`; review sign-off recorded in the approval thread | Reviewer, demo step 3 |
| 3 | "the full e-commerce manifest example validates by inspection against the draft manifest schema" | The step-5 checklist passes in full: schema conformance, bounds, sums, reachability argument, zero hooks | Reviewer, demo step 5 |
| 4 | "every phase doc has concrete exit criteria and a demo script" | All 13 phase docs contain both sections, with named proving suites in every exit-criteria row | Reviewer, demo step 6 |
| 5 | Approval gate honored (binding consequence) | Zero application code in the repo at gate time; work stops until the user approves | Reviewer, demo step 7 |
