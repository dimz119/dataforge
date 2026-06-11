# ADR-0001 — Monorepo layout

**Deliverable:** D17

DataForge ships as a single repository containing the Django backend, the React frontend, the infrastructure definitions, and these specs. This warranted an ADR because the repo split is the first structural decision a greenfield project makes, it is awkward to reverse once CI, deployment, and contributor habits calcify around it, and DataForge's contract artifacts (OpenAPI schema, manifest JSON Schema, envelope JSON Schema) deliberately couple the two applications.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** repository layout from Phase 1 onward; CI pipeline shape; contract-artifact publication

## Context

The forces:

- **The contracts cross the app boundary by design.** ADR-0014 makes the OpenAPI schema a CI artifact consumed by a generated TypeScript client (ADR-0016), so any backend endpoint change must compile against frontend usage in the same change set. The manifest JSON Schema ([../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §9.1) and the envelope JSON Schema ([../03-domain/event-model.md](../03-domain/event-model.md) §1) are likewise CI artifacts validated against fixtures on both sides. Contract drift failing the build is a stated requirement, not a nicety.
- **Phased delivery with review gates.** The roadmap ([../07-plan/incremental-roadmap.md](../07-plan/incremental-roadmap.md)) delivers in 13 small reviewable phases; most phases touch backend, frontend, and infra together (e.g. Phase 7 wires the generated client into CI; Phase 9 ships chaos APIs plus a console panel). Reviewing one PR per phase is only possible if one repo holds the whole diff.
- **One image, several process groups.** ADR-0015 deploys web/ws/worker/runner from a single image; a single build context is the natural fit.
- **Small team, greenfield, empty repo.** There is no organizational boundary (separate teams, separate release cadences, partial access control) that would justify repo separation; `/Users/seungjoonlee/git/dataforge` is empty with zero commits.

## Decision

One repository with four top-level trees:

| Tree | Contents |
|---|---|
| `backend/` | Django + DRF project; one Django app per bounded context per [../03-domain/domain-model.md](../03-domain/domain-model.md) §1.3; Celery workers and runner entrypoints; builtin scenario manifests as data files |
| `frontend/` | Vite + React + TypeScript SPA (ADR-0016), including the generated OpenAPI client output |
| `infra/` | Docker Compose dev stack, Fly.io configuration, CI workflow definitions, deployment scripts |
| `specs/` | This documentation tree — the 20 deliverables, ADRs, and phase docs, organized in grouped subdirectories with an explicit D1–D20 → file map in `specs/README.md` |

Operating rules:

1. **One CI pipeline, path-filtered jobs.** Backend paths trigger `ruff` + `mypy` + `pytest`; frontend paths trigger `eslint` + `tsc` + `vitest`; either triggers the contract jobs. Defined in Phase 1 ([../07-plan/phases/phase-01-foundations.md](../07-plan/phases/phase-01-foundations.md)).
2. **Contract artifacts are built in CI on every PR:** the drf-spectacular OpenAPI schema, the regenerated TypeScript client (a dirty diff after regeneration fails the build), the manifest JSON Schema, and the envelope JSON Schema with its golden fixtures. The repo is the single source of truth for all four; none is published to an external registry in MVP.
3. **Specs layout:** grouped subdirectories (`01-product/` … `07-plan/`, `adr/`) with strict one-deliverable-to-one-file mapping and a status table in `specs/README.md` — the panel synthesis of P1's traceability discipline with P2/P3's grouping.
4. The exact tree down to Django-app and React-feature-folder level is owned by [../07-plan/project-folder-structure.md](../07-plan/project-folder-structure.md) (D19); this ADR fixes only the top level and the lockstep-contract rule.

## Alternatives considered

- **Two repositories (backend, frontend).** The conventional SaaS split. Rejected: every envelope, manifest-schema, or endpoint change becomes a cross-repo coordination problem — the generated-client check would need cross-repo triggers and version pinning, exactly the drift the requirements forbid; phase review gates would span two PRs that can merge independently and inconsistently. The benefits (independent release cadence, team-scoped access) serve organizational boundaries DataForge does not have.
- **Three repos plus a dedicated contracts repo.** Makes the contract artifacts first-class but triples the coordination cost: a contract change now requires a three-repo dance with an ordering constraint. Appropriate at many-team scale; pure overhead here.
- **Monorepo with workspace/build-graph tooling (Nx, Bazel, Pants).** Rejected for MVP: two applications and one infra tree do not need a build graph; GitHub Actions path filters deliver the same selective-build property at zero tooling cost. Revisit only if the app count grows materially (a future ADR).
- **Flat specs files instead of grouped directories** (panel position P1; P2/P3 held grouped). Resolved: 23+ docs flat is unwieldy, so grouped won — but P1's discipline (one deliverable ↔ one file, explicit status table) was adopted wholesale because reviewer traceability to the 20 required deliverables must stay mechanical.

## Consequences

### Positive

- Contract changes are atomic: envelope, OpenAPI, generated client, and consuming UI code land in one reviewable diff, and CI proves they agree before merge.
- One pipeline, one issue tracker, one history; each phase is one reviewable unit, matching the demo-script-per-phase convention.
- The single image build for Fly process groups (ADR-0015) falls out naturally.

### Negative

- CI wall time grows with the repo; mitigated by path filtering and job parallelism, accepted as the cost of lockstep contracts.
- No per-tree access control: anyone with repo access sees everything. Acceptable for the current team shape; a future organizational split would require repo surgery (the known cost of this door).
- Git history interleaves backend, frontend, infra, and specs commits; conventional commit scopes (`backend:`, `frontend:`, `infra:`, `specs:`) keep it filterable.

### Follow-ups

- [../07-plan/project-folder-structure.md](../07-plan/project-folder-structure.md) (D19) specifies the full tree; Phase 1 exit criteria verify the scaffold matches it.
- Phase 1 implements the path-filtered CI pipeline and all four contract-artifact jobs.
- The pre-commit configuration (ruff, eslint, formatting) is defined in Phase 1 alongside CI so local and CI checks never diverge.
