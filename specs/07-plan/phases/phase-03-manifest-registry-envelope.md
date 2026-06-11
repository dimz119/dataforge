# Phase 3 — Manifest Contract, Schema Registry, Envelope

**Deliverable:** D18 (phase doc)

Phase 3 implements the three Phase-0-frozen contracts that everything downstream consumes: the manifest JSON Schema v0 with its validator, the in-house schema registry with subject derivation, and the canonical event envelope library. It lands **before any generation code** so the runtime is generic from its first line — the e-commerce scenario enters the system purely as data, and a CI grep proves it stays that way forever.

## Goal

> The declarative scenario contract and registry exist before any generation code, so the runtime is generic from its first line.

## Dependencies

| Dependency | Role |
|---|---|
| Phase 2 complete | Auth for catalog/registry APIs; tenancy stack for workspace-visibility manifests (INV-CAT-6); audit writer |
| [../../04-engines/scenario-plugin-architecture.md](../../04-engines/scenario-plugin-architecture.md) | Manifest v0 schema (§9.1), validation pipeline (§8), catalog/loader (§10), overlay + pinning (§11), AI slot-in contract (§12) |
| [../../04-engines/schema-registry.md](../../04-engines/schema-registry.md) | Subjects, `BACKWARD_ADDITIVE` gate, read/write API (A1–A8), derivation transaction |
| [../../03-domain/event-model.md](../../03-domain/event-model.md) | Envelope 1.0 field catalog, serialization rules S-1…S-6, deterministic UUIDv7, `partition_key` PK-1…3 |
| [../../03-domain/database-schema.md](../../03-domain/database-schema.md) | DDL for `scenarios`, `manifest_versions`, scenario instances, registry subjects/versions |
| [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md) | Catalog/registry endpoint shapes, 422 problem-details, async-job polling |
| ADR-0003 (declarative manifests), ADR-0010 (in-house registry), ADR-0004 (envelope) | Structural decisions implemented here |

## Scope

- **Manifest JSON Schema v0** versioned in-repo (`backend/catalog/schema/manifest-v0.schema.json`) and published as a CI artifact; hardened parse before any layer (YAML safe-load, anchors/aliases rejected, ≤ 512 KiB, depth ≤ 12 → MAN-S001/S002/S003).
- **Validator layers 1+2** with actionable errors `{code, path (JSON Pointer), message, bound, actual, scope}`: L1 schema conformance (MAN-S004); L2 semantic checks — referential integrity MAN-V1xx, probability/machine structure MAN-V2xx (sums V201, orphan states V204, escape-less SCC V205, expected-steps ≤ 1,000 via the fundamental matrix V207), resource bounds MAN-V3xx (B-01…B-17), generator allowlist + params MAN-V4xx, schema-compat MAN-V5xx. **Layer 3 (dry run) is Phase 4** — the only manifests published in the window are builtins, which Phase 4 CI retroactively re-validates through L3 (scenario-plugin §8.4 sequencing).
- **Scenario catalog:** `Scenario`/`ManifestVersion` models with the draft → published → deprecated state machine (INV-CAT-1/2/5); `sync_builtin_scenarios` command with sha256 no-op / hard-fail semantics (§10.2); catalog APIs — list, detail, version detail, validation-report polling, publish (`POST /scenarios/{slug}/versions/{manifest_version}/publish`, api-spec #32), workspace-visibility document ingestion per the §12 slot-in (validator quotas AI-4 enforced); `ScenarioInstance` model with the configuration overlay, merged-document L2 re-validation, and `config_revision` counter; pinning fields per PIN-1…PIN-5.
- **E-commerce subset manifest 1.0.0 as data** at `backend/catalog/builtin/ecommerce/1.0.0.yaml`: entities `users`, `products`, `orders`, `payments`; purchase-funnel session machine (PRD F1–F5 subset). The full 8-entity manifest exists only as the Phase-0 spec example until Phase 8.
- **Schema registry app (ADR-0010):** `Subject`/`SchemaVersion` models (INV-REG-1/2), `BACKWARD_ADDITIVE` compatibility gate (INV-REG-3), read API A1–A5 + write A7 + check A8, publish-transaction derivation R-DER-1…5 — publishing a manifest version derives and registers v1 JSON Schemas for every subject in the same transaction.
- **Envelope library:** all 20 fields + internal `_df` block; canonical serialization S-2; deterministic UUIDv7 (`occurred_at` ms timestamp bits + seeded-PRNG random bits, §2.2.1); `partition_key` derivation PK-1…3; envelope 1.0 JSON Schema generated as a CI artifact and golden-fixture-tested against [../../03-domain/event-model.md](../../03-domain/event-model.md) §2.1 (EV-6). The CON field-set pin becomes a permanent gate here.

## Non-goals

| Deferred | Lands in |
|---|---|
| Manifest interpretation / any event generation | Phase 4 |
| Validator layer 3 (dry run, MAN-D6xx) | Phase 4 (with the behavior engine that executes it) |
| Full 8-entity manifest registration; CDC subjects beyond what the subset derives | Phase 8 |
| Registry browser UI, diff API (A6), mid-stream schema upgrades | Phase 10 |
| Streams pinning anything (the pin model exists; the Stream resource is Phase 5) | Phase 5 |
| Prompt→manifest generation service and manifest-upload console UX (the API ingestion path + validation pipeline ship now per §12) | Post-MVP (Phase 12+) |

## Tasks

- [ ] Manifest v0 JSON Schema file + CI artifact job; hardened parse front-end (MAN-S001–S003)
- [ ] L1 validation (MAN-S004) with JSON Pointer error paths
- [ ] L2 referential checks MAN-V101–V110
- [ ] L2 machine-structure checks MAN-V201–V211 incl. SCC escape analysis (V205) and the `(I−Q)⁻¹` expected-steps bound (V207)
- [ ] L2 resource bounds MAN-V301–V317 (B-01…B-17) + generator allowlist/params MAN-V401–V406
- [ ] ValidationReport model + persistence on ManifestVersion (scenario-plugin §8.3 shape)
- [ ] Catalog models + draft/published/deprecated lifecycle (INV-CAT-1/2/5) + audit entries
- [ ] `sync_builtin_scenarios` command (insert / sha-match no-op / sha-mismatch hard fail) wired into the deploy entrypoint
- [ ] Catalog APIs: list/detail/versions/validation/publish + workspace-visibility ingestion with AI-4 quotas
- [ ] ScenarioInstance + overlay validation (merged-doc L2 re-run, `scope: "override"` errors, `config_revision`)
- [ ] E-commerce subset manifest `1.0.0` YAML (zero hooks, PRD §4.1 F1–F5 defaults)
- [ ] Registry models + `BACKWARD_ADDITIVE` checker (INV-REG-2/3) + subject naming (INV-REG-1)
- [ ] Registry read API A1–A5, write A7, check A8; per-workspace subject quota hooks
- [ ] Publish-transaction schema derivation R-DER-1…5 (deterministic, byte-identical re-derivation)
- [ ] Envelope library: fields, S-1…S-6 serialization, deterministic UUIDv7, PK-1…3; envelope JSON Schema CI artifact + CON field-set pin test
- [ ] GUARD greps land permanently: no-ecommerce-logic-in-Python, no-hooks-in-reference-manifest
- [ ] Adversarial manifest fixture corpus (≥ 1 fixture per MAN-S/V code) per testing-strategy §16.3

## Demo script

1. `docker compose -f infra/compose/compose.yaml up -d --wait` — the entrypoint migration runs `sync_builtin_scenarios`; logs show `ecommerce 1.0.0 published`.
2. Auth: run `infra/scripts/demo-phase02.sh` to obtain `$ACCESS` and `$WS`.
3. `curl -s localhost:8000/api/v1/scenarios -H "Authorization: Bearer $ACCESS" | jq '.data[].scenario_slug'` → `"ecommerce"`.
4. `curl -s localhost:8000/api/v1/scenarios/ecommerce/versions/1.0.0 -H "Authorization: Bearer $ACCESS" | jq '.validation_report.status'` → `"passed"`.
5. Reject a probability-sum violation: POST a copy of the subset manifest with `checkout_started` probabilities summing to 1.15 to `/api/v1/scenarios` → `422` problem-details; `jq '.errors[0]'` shows `code: "MAN-V201"`, JSON Pointer `/state_machines/shopping_session/states/checkout_started`, `bound: 1.0`, `actual: 1.15`.
6. Reject an escape-less cycle: POST a manifest whose two non-terminal states only transition to each other → `422` with `MAN-V205` naming the SCC states.
7. Registry subjects: `curl -s 'localhost:8000/api/v1/schemas/subjects?scenario=ecommerce' -H "Authorization: Bearer $ACCESS" | jq '.data[].subject'` → `ecommerce.user_registered`, `ecommerce.order_placed`, … (every subset event type).
8. Versions: `curl -s localhost:8000/api/v1/schemas/subjects/ecommerce.order_placed/versions -H "Authorization: Bearer $ACCESS" | jq '.data[0].version'` → `1`; `…/versions/1/schema` returns the derived JSON Schema with `additionalProperties: false` and every field required (R-DER-3).
9. Compat gate: `POST …/subjects/ecommerce.order_placed/versions:check` with a schema that removes `total` → `200 {"compatible": false}` naming the removed field.
10. Envelope round-trip: `pytest backend/tests/contract/test_envelope_pin.py -q` — builds an `order_placed` envelope with `schema_ref {subject: "ecommerce.order_placed", version: 1}`, serializes canonically, validates against the envelope 1.0 artifact, parses back byte-identically.
11. Genericity guard: `grep -rn ecommerce backend --include='*.py' | grep -v catalog/builtin` → empty; CI shows the GUARD job green.

## Exit criteria

Binding text with measurable assertions; proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 3 rows).

| # | Binding criterion | Measurable assertion | Proving suite (lane) |
|---|---|---|---|
| 1 | "Malformed manifests (incl. probability-sum and cycle violations) rejected with precise errors" | Every MAN-S/V code has a failing fixture asserting `{code, path, message, bound, actual}`; generated manifest mutations match the validator oracle | UNIT validator fixtures + PROP-SM-2 (PR) |
| 2 | "registering the manifest auto-derives v1 event schemas" | Publishing `ecommerce 1.0.0` registers version 1 for every derived subject in the same transaction; re-derivation is byte-identical | CON §8.1 + registry integration tests (PR) |
| 3 | "GET /api/v1/scenarios and /schemas/{subject}/versions work" | Demo steps 3–8 pass; responses validate against the OpenAPI artifact; errors are RFC 9457 | CON §8.1 schemathesis (PR) |
| 4 | "envelope round-trips with schema_ref stamped" | Serialized samples validate against the envelope 1.0 JSON Schema artifact; exact 20-key delivered set pinned; canonical serialization byte-stable | CON §8.2 field-set pin (PR, permanent) |
| 5 | "zero e-commerce logic in Python" | `grep -r ecommerce backend/ --include='*.py'` matches nothing outside `catalog/builtin/`; reference manifest contains zero `hook` generators | GUARD greps §8.4 (PR, permanent) |
