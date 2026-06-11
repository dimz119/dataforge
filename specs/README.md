# DataForge — Design Specifications Index

DataForge is a multi-tenant SaaS synthetic-data platform — "a realistic data engineering playground that behaves like a real company." It generates referentially valid business data from declarative scenario manifests and continuously streams events (with configurable chaos: duplicates, late arrivals, missing events, out-of-order delivery, corrupted values, nulls, schema drift) so data engineers, analytics engineers, students, and instructors can practice Kafka, Spark, Flink, Airflow, dbt, Iceberg, warehousing, and CDC against data that misbehaves the way production data does. This directory is the complete Phase 0 design: all twenty required deliverables (D1–D20), seventeen Architecture Decision Records, and thirteen implementation phase docs. Every document below was authored against the binding decisions of the design panel; alternatives considered live only inside the ADRs.

---

## 1. Traceability: deliverables D1–D20 → files

Every required deliverable maps to one or more committed files. There are no orphan deliverables and no unmapped files.

| # | Deliverable | File(s) | Scope |
|---|---|---|---|
| D1 | PRD | [01-product/prd.md](01-product/prd.md) | Personas, JTBD, core flow, concrete realism contract (funnel %s, lifecycle latencies, intensity curves), exercise catalog, success metrics, NFR summary |
| D2 | System Architecture Diagram | [02-architecture/system-architecture.md](02-architecture/system-architecture.md) | C4 context/container views, control-plane vs data-plane split, internal Kafka backbone and delivery-adapter seam, user consumption boundary, process inventory, failure domains |
| D3 | Domain Model | [03-domain/domain-model.md](03-domain/domain-model.md) | Bounded-context map, aggregates and invariants (`INV-*` IDs), stream and API-key lifecycles, ubiquitous language glossary (terminology authority for the whole tree) |
| D4 | Database Schema | [03-domain/database-schema.md](03-domain/database-schema.md) | Full PostgreSQL DDL: every table, index, time-partitioning/retention for ledger/buffer/injections/audit, cursor encoding, RLS policies, migration policy |
| D5 | Event Model | [03-domain/event-model.md](03-domain/event-model.md) | Frozen canonical envelope (UUIDv7, dual timestamps, clock-domain rules, `schema_ref`), Debezium-shaped CDC sub-envelope, ground-truth label strip boundary, per-channel guarantee table |
| D6 | Scenario Plugin Architecture | [04-engines/scenario-plugin-architecture.md](04-engines/scenario-plugin-architecture.md) + [04-engines/scenarios/ecommerce.md](04-engines/scenarios/ecommerce.md) | Manifest v0 JSON Schema, generator vocabulary, three-layer validation with resource bounds, version pinning, AI-manifest slot-in contract and threat model; plus the full 8-entity e-commerce manifest as the DSL expressiveness forcing function |
| D7 | Behavior Engine Design | [04-engines/behavior-engine.md](04-engines/behavior-engine.md) | Per-actor state-machine interpreter, three-tier entity pools, guard evaluation, virtual clock, dwell times, intensity curves, seed-derivation tree, backfill, checkpoint/restore |
| D8 | Chaos Engine Design | [04-engines/chaos-engine.md](04-engines/chaos-engine.md) | Seven failure modes as a seeded ordered composable post-ledger transform, per-stream config schema, durable late-arrival buffer with full lifecycle matrix, injection recording, delivery-truth vs business-truth doctrine |
| D9 | Schema Registry Design | [04-engines/schema-registry.md](04-engines/schema-registry.md) | Postgres + JSON Schema registry, Confluent-compatible subjects, `BACKWARD_ADDITIVE` compatibility algorithm, v1/v2/v3 evolution trio, mid-stream upgrade, drift-mode linkage, Confluent SR mirroring path |
| D10 | API Specifications | [05-interfaces/api-specification.md](05-interfaces/api-specification.md) | Complete `/api/v1` endpoint catalog with request/response shapes and status codes, RFC 9457 problem-details catalog, cursor pagination, rate limits, versioned WebSocket protocol, OpenAPI CI pipeline |
| D11 | Frontend Architecture | [02-architecture/frontend-architecture.md](02-architecture/frontend-architecture.md) | Vite + React + TS SPA, feature folders, routing map, TanStack Query conventions, generated OpenAPI client, WS hook layer, token model, all seven page groups |
| D12 | Backend Architecture | [02-architecture/backend-architecture.md](02-architecture/backend-architecture.md) | Django apps as bounded contexts, clean layering with CI enforcement, request lifecycle, Celery control-plane topology, runner data-plane process, Channels ASGI group, internal Kafka topology |
| D13 | Deployment Architecture | [02-architecture/deployment-architecture.md](02-architecture/deployment-architecture.md) | Docker Compose dev stack (nine services), Fly.io process groups, Kafka placement + managed-migration trigger, secrets, environment promotion, CI/CD, backup/restore |
| D14 | Security Architecture | [06-quality/security-architecture.md](06-quality/security-architecture.md) | Threat model, JWT/API-key auth duality, three-layer tenancy enforcement, full account lifecycle and signup abuse controls, untrusted-manifest threat model, audit logging, secrets, `SEC-*` rule IDs |
| D15 | Scaling Strategy | [02-architecture/scaling-strategy.md](02-architecture/scaling-strategy.md) | Capacity arithmetic for 1 → 100,000 TPS: per-component unit capacities, six-rung TPS staircase with named bottleneck and remedy per rung, backpressure policy, quotas, availability reconciliation |
| D16 | Testing Strategy | [06-quality/testing-strategy.md](06-quality/testing-strategy.md) | Full test taxonomy (property/invariant, statistical-with-tolerance, golden-seed replay, cross-tenant attack, cross-channel contract, soak/load), permanent CI gates, per-phase gate table, `INV-*` bindings |
| D17 | ADR Documents | [adr/](adr/README.md) — index + 17 ADRs (full list in §4) | One file per architecturally significant decision with context, decision, alternatives considered, consequences; six marked review-blocking one-way doors |
| D18 | Incremental Development Roadmap | [07-plan/incremental-roadmap.md](07-plan/incremental-roadmap.md) + [07-plan/phases/](07-plan/phases/README.md) — index + 13 phase docs (full list in §4) | Phase sequence 0–12 with dependency graph and review-gate convention; one binding work order per phase (goal, scope, non-goals, demo script, exit criteria) |
| D19 | Project Folder Structure | [07-plan/project-folder-structure.md](07-plan/project-folder-structure.md) | Monorepo tree (`backend/`, `frontend/`, `infra/`, `specs/`) fixed down to Django-app and React-feature-folder level; folder-lint authority for the Phase 1 scaffold |
| D20 | MVP vs Future Roadmap | [07-plan/mvp-vs-future.md](07-plan/mvp-vs-future.md) | MVP cut line (Phases 0–11 = GA) with the single cut rule (one-way-door seams in, instances-through-seams out), deferred backlog, contract-freeze list |

### Supporting specs (no standalone deliverable number)

| File | Supports | Scope |
|---|---|---|
| [02-architecture/observability.md](02-architecture/observability.md) | D2, D13, D15 | Structured log schema, metrics catalog, health/readiness semantics, SLO definitions (control vs data plane — what "99.9%" measures), error budget, alert and audit-event catalogs, OTel path |
| [04-engines/delivery-channels.md](04-engines/delivery-channels.md) | D2, D5, D15 | `DeliveryChannel` sink contract, REST cursor + WS tail specs, Phase 12 hosted-Kafka and webhook contracts, S3/Iceberg/CDC-export file/commit semantics frozen now |

---

## 2. Recommended reading order

Foundations first; each tier assumes the tiers above it.

1. **Product** — [01-product/prd.md](01-product/prd.md): what is being built and the realism contract everything else implements.
2. **Domain** — [03-domain/domain-model.md](03-domain/domain-model.md): the terminology authority; read before anything else that uses a domain term.
3. **Structure** — [02-architecture/system-architecture.md](02-architecture/system-architecture.md): the structural map; then the six review-blocking one-way-door ADRs ([adr-0002](adr/adr-0002-multi-tenancy-model.md), [adr-0003](adr/adr-0003-declarative-scenario-manifests.md), [adr-0004](adr/adr-0004-canonical-event-envelope.md), [adr-0005](adr/adr-0005-internal-kafka-backbone.md), [adr-0009](adr/adr-0009-staged-pipeline-ground-truth-then-chaos.md), [adr-0010](adr/adr-0010-inhouse-schema-registry.md)).
4. **Frozen contracts** — [03-domain/event-model.md](03-domain/event-model.md), then [04-engines/scenario-plugin-architecture.md](04-engines/scenario-plugin-architecture.md) with its worked example [04-engines/scenarios/ecommerce.md](04-engines/scenarios/ecommerce.md).
5. **Engines** — [04-engines/behavior-engine.md](04-engines/behavior-engine.md), [04-engines/chaos-engine.md](04-engines/chaos-engine.md), [04-engines/schema-registry.md](04-engines/schema-registry.md), [04-engines/delivery-channels.md](04-engines/delivery-channels.md).
6. **Persistence and interface** — [03-domain/database-schema.md](03-domain/database-schema.md), [05-interfaces/api-specification.md](05-interfaces/api-specification.md).
7. **Application architecture** — [02-architecture/backend-architecture.md](02-architecture/backend-architecture.md), [02-architecture/frontend-architecture.md](02-architecture/frontend-architecture.md).
8. **Quality** — [06-quality/security-architecture.md](06-quality/security-architecture.md), [06-quality/testing-strategy.md](06-quality/testing-strategy.md).
9. **Operations** — [02-architecture/deployment-architecture.md](02-architecture/deployment-architecture.md), [02-architecture/scaling-strategy.md](02-architecture/scaling-strategy.md), [02-architecture/observability.md](02-architecture/observability.md).
10. **Plan** — [07-plan/incremental-roadmap.md](07-plan/incremental-roadmap.md), [07-plan/mvp-vs-future.md](07-plan/mvp-vs-future.md), [07-plan/project-folder-structure.md](07-plan/project-folder-structure.md), then the per-phase docs under [07-plan/phases/](07-plan/phases/README.md).
11. **Remaining ADRs** — [adr/README.md](adr/README.md) and the eleven non-blocking ADRs, consulted on demand as the specs cite them.

---

## 3. Document status

All documents are at the same gate. Decision status *inside* each ADR (Accepted) records the panel's decision; the document review status below is the user design review, which has not yet occurred.

| Document | Status |
|---|---|
| 01-product/prd.md | Draft — awaiting design review |
| 02-architecture/system-architecture.md | Draft — awaiting design review |
| 02-architecture/backend-architecture.md | Draft — awaiting design review |
| 02-architecture/frontend-architecture.md | Draft — awaiting design review |
| 02-architecture/deployment-architecture.md | Draft — awaiting design review |
| 02-architecture/scaling-strategy.md | Draft — awaiting design review |
| 02-architecture/observability.md | Draft — awaiting design review |
| 03-domain/domain-model.md | Draft — awaiting design review |
| 03-domain/database-schema.md | Draft — awaiting design review |
| 03-domain/event-model.md | Draft — awaiting design review |
| 04-engines/scenario-plugin-architecture.md | Draft — awaiting design review |
| 04-engines/scenarios/ecommerce.md | Draft — awaiting design review |
| 04-engines/behavior-engine.md | Draft — awaiting design review |
| 04-engines/chaos-engine.md | Draft — awaiting design review |
| 04-engines/schema-registry.md | Draft — awaiting design review |
| 04-engines/delivery-channels.md | Draft — awaiting design review |
| 05-interfaces/api-specification.md | Draft — awaiting design review |
| 06-quality/security-architecture.md | Draft — awaiting design review |
| 06-quality/testing-strategy.md | Draft — awaiting design review |
| 07-plan/incremental-roadmap.md | Draft — awaiting design review |
| 07-plan/mvp-vs-future.md | Draft — awaiting design review |
| 07-plan/project-folder-structure.md | Draft — awaiting design review |
| 07-plan/phases/README.md + phase-00 … phase-12 (14 docs) | Draft — awaiting design review |
| adr/README.md + adr-0001 … adr-0017 (18 docs) | Draft — awaiting design review |

---

## 4. Complete file inventory for D17 and D18

**D17 — ADRs** ([adr/README.md](adr/README.md) is the index; review-blocking one-way doors marked ◆):

| ADR | File |
|---|---|
| ADR-0001 Monorepo layout | [adr/adr-0001-monorepo.md](adr/adr-0001-monorepo.md) |
| ADR-0002 Multi-tenancy model ◆ | [adr/adr-0002-multi-tenancy-model.md](adr/adr-0002-multi-tenancy-model.md) |
| ADR-0003 Declarative scenario manifests ◆ | [adr/adr-0003-declarative-scenario-manifests.md](adr/adr-0003-declarative-scenario-manifests.md) |
| ADR-0004 Canonical event envelope ◆ | [adr/adr-0004-canonical-event-envelope.md](adr/adr-0004-canonical-event-envelope.md) |
| ADR-0005 Internal Kafka backbone ◆ | [adr/adr-0005-internal-kafka-backbone.md](adr/adr-0005-internal-kafka-backbone.md) |
| ADR-0006 Celery control plane / runner data plane | [adr/adr-0006-celery-control-plane-runner-data-plane.md](adr/adr-0006-celery-control-plane-runner-data-plane.md) |
| ADR-0007 Behavior engine state machines | [adr/adr-0007-behavior-engine-state-machines.md](adr/adr-0007-behavior-engine-state-machines.md) |
| ADR-0008 Deterministic seeds + virtual clock | [adr/adr-0008-deterministic-seeds-virtual-clock.md](adr/adr-0008-deterministic-seeds-virtual-clock.md) |
| ADR-0009 Staged pipeline: ground truth then chaos ◆ | [adr/adr-0009-staged-pipeline-ground-truth-then-chaos.md](adr/adr-0009-staged-pipeline-ground-truth-then-chaos.md) |
| ADR-0010 In-house schema registry ◆ | [adr/adr-0010-inhouse-schema-registry.md](adr/adr-0010-inhouse-schema-registry.md) |
| ADR-0011 Auth duality: JWT + API keys | [adr/adr-0011-auth-duality-jwt-and-api-keys.md](adr/adr-0011-auth-duality-jwt-and-api-keys.md) |
| ADR-0012 State-first generation + CDC | [adr/adr-0012-state-first-generation-cdc.md](adr/adr-0012-state-first-generation-cdc.md) |
| ADR-0013 REST cursor buffer / WS tail | [adr/adr-0013-rest-cursor-buffer-ws-tail.md](adr/adr-0013-rest-cursor-buffer-ws-tail.md) |
| ADR-0014 API conventions | [adr/adr-0014-api-conventions.md](adr/adr-0014-api-conventions.md) |
| ADR-0015 Fly.io process groups + Kafka placement | [adr/adr-0015-flyio-process-groups-kafka-placement.md](adr/adr-0015-flyio-process-groups-kafka-placement.md) |
| ADR-0016 Frontend stack | [adr/adr-0016-frontend-stack.md](adr/adr-0016-frontend-stack.md) |
| ADR-0017 Instructor answer key ◆* | [adr/adr-0017-instructor-answer-key.md](adr/adr-0017-instructor-answer-key.md) |

\* ADR-0017 is Accepted but not review-blocking; only ADRs 0002/0003/0004/0005/0009/0010 block design approval.

**D18 — Phase docs** ([07-plan/phases/README.md](07-plan/phases/README.md) is the index):

| Phase | File |
|---|---|
| 0 — Design specs | [07-plan/phases/phase-00-specs.md](07-plan/phases/phase-00-specs.md) |
| 1 — Foundations | [07-plan/phases/phase-01-foundations.md](07-plan/phases/phase-01-foundations.md) |
| 2 — Identity, tenancy, API keys, audit | [07-plan/phases/phase-02-identity-tenancy.md](07-plan/phases/phase-02-identity-tenancy.md) |
| 3 — Manifest contract + registry + envelope | [07-plan/phases/phase-03-manifest-registry-envelope.md](07-plan/phases/phase-03-manifest-registry-envelope.md) |
| 4 — Generation core + batch datasets | [07-plan/phases/phase-04-generation-core-batch.md](07-plan/phases/phase-04-generation-core-batch.md) |
| 5 — Streaming runtime | [07-plan/phases/phase-05-streaming-runtime.md](07-plan/phases/phase-05-streaming-runtime.md) |
| 6 — Stream control surface | [07-plan/phases/phase-06-stream-control.md](07-plan/phases/phase-06-stream-control.md) |
| 7 — Console MVP | [07-plan/phases/phase-07-console-mvp.md](07-plan/phases/phase-07-console-mvp.md) |
| 8 — Full e-commerce + CDC + realism | [07-plan/phases/phase-08-full-ecommerce-cdc.md](07-plan/phases/phase-08-full-ecommerce-cdc.md) |
| 9 — Chaos engine + answer key | [07-plan/phases/phase-09-chaos-engine.md](07-plan/phases/phase-09-chaos-engine.md) |
| 10 — Schema evolution exercises | [07-plan/phases/phase-10-schema-evolution.md](07-plan/phases/phase-10-schema-evolution.md) |
| 11 — Scale + hardening (MVP GA) | [07-plan/phases/phase-11-scale-hardening.md](07-plan/phases/phase-11-scale-hardening.md) |
| 12 — Delivery expansion (post-MVP) | [07-plan/phases/phase-12-delivery-expansion.md](07-plan/phases/phase-12-delivery-expansion.md) |

---

## 5. Glossary

The ubiquitous language for the entire specs tree — every domain term, exactly as used in all documents — is defined once, in **[03-domain/domain-model.md](03-domain/domain-model.md) §6 (Ubiquitous Language)**. No other document redefines a term; when in doubt about what a *stream*, *runner*, *shard*, *manifest*, *subject*, *cursor*, or *injection* is, that section is authoritative.

---

## 6. Approval gate

Phase 0 ends at a hard gate: once this specs tree is committed, **work stops for user design review**. No application code is written before design approval. The six review-blocking ADRs (0002, 0003, 0004, 0005, 0009, 0010 — the one-way doors) must be reviewed and remain Accepted for the gate to pass; all other documents may take non-blocking review feedback into Phase 1 without holding the gate. The full gate contract is in [07-plan/phases/phase-00-specs.md](07-plan/phases/phase-00-specs.md).
