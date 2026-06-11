# DataForge — Create `specs/` with all 20 design deliverables + implementation phases

## Context

The repo `/Users/seungjoonlee/git/dataforge` is empty (no commits). The goal is **DataForge**: a multi-tenant SaaS synthetic-data platform that behaves like a real company — entity relationships, realistic event funnels, CDC, schema evolution, and injected data-quality failures — so data engineers, students, and instructors can practice Kafka/Spark/Flink/Airflow/DBT/Iceberg/warehouse pipelines against it.

The instruction is **design-first**: produce all 20 design deliverables and an incremental implementation roadmap before any code. This plan covers authoring the `specs/` directory only. **Per user decision: after specs are written, STOP for explicit review/approval — no code until the user approves the design.**

Plan was pressure-tested by a 3-perspective design panel (pragmatic-shipper / platform-architect / educator-product) + adversarial critic synthesis.

## What gets created (all under `specs/`, ~40 markdown files, no code)

```
specs/
  README.md                      # D1–D20 → file traceability map, reading order, doc status table
  01-product/prd.md              # [D1] personas, JTBD, concrete realism criteria (funnel %, diurnal curves), exercise catalog
  02-architecture/
    system-architecture.md       # [D2] C4/Mermaid; control plane vs data plane; Kafka backbone → delivery adapters
    backend-architecture.md      # [D12] Django apps as DDD bounded contexts; api→application→domain→infra layering
    frontend-architecture.md     # [D11] Vite+React+TS SPA, TanStack Query, OpenAPI-generated client, WS hooks
    deployment-architecture.md   # [D13] Compose dev stack; Fly.io process groups (web/ws/worker/runner); Kafka placement
    scaling-strategy.md          # [D15] 1→100k TPS staircase WITH capacity arithmetic per rung
    observability.md             # log schema, metrics catalog, health checks, SLO definitions, OTel path
  03-domain/
    domain-model.md              # [D3] bounded contexts, aggregates, invariants, ubiquitous language
    database-schema.md           # [D4] ERD/DDL: workspaces, api_keys, streams, schema_versions, event_buffer
                                 #      (time-partitioned), ground-truth ledger, chaos_injections, audit_log; RLS
    event-model.md               # [D5] canonical envelope (UUIDv7, dual timestamps + clock-domain rules,
                                 #      schema_ref, partition_key); Debezium-shaped CDC (op/before/after)
  04-engines/
    scenario-plugin-architecture.md  # [D6] declarative manifest JSON Schema; validation (resource bounds,
                                     #      probability sums, reachability); AI-manifest threat model
    behavior-engine.md           # [D7] per-actor state machines, entity pools, guards, seeds, virtual clock
    chaos-engine.md              # [D8] 7 failure modes as seeded post-ledger pipeline stages; late-arrival
                                 #      buffer lifecycle semantics; answer-key recording
    schema-registry.md           # [D9] Postgres+JSON Schema, Confluent-compatible subjects, v1/v2/v3 exercise
    delivery-channels.md         # DeliveryChannel sink contract; REST/WS now; Kafka/webhook/S3/Iceberg/CDC contracts
    scenarios/ecommerce.md       # full 8-entity reference manifest worked example (DSL forcing function)
  05-interfaces/api-specification.md  # [D10] /api/v1 catalog, cursor pagination, RFC 9457 errors, WS protocol
  06-quality/
    security-architecture.md     # [D14] JWT vs hashed API keys, 3-layer tenancy enforcement, account lifecycle
    testing-strategy.md          # [D16] property/invariant tests, statistical tolerances, golden-seed replay,
                                 #      cross-tenant attack suite as permanent CI gate
  07-plan/
    incremental-roadmap.md       # [D18] phase sequence + dependency graph + review gates
    mvp-vs-future.md             # [D20] cut rule: one-way-door seams IN MVP; instances-through-seams deferred
    project-folder-structure.md  # [D19] monorepo: backend/ frontend/ infra/ specs/
    phases/                      # README + 13 phase docs (below), each: goal, scope, non-goals, demo script, exit criteria
  adr/                           # [D17] README + 17 ADRs (below)
```

## Data consumption model (user-confirmed)

Users never touch DataForge's internal Kafka — it is server-side infrastructure (compose service in dev; internal-only single-broker KRaft on Fly.io in prod). **MVP consumption: users pull from hosted DataForge over the internet via cursor-based REST and WebSocket using an API key; bridging events into their own local Kafka is their exercise (connection guides provided).** Post-MVP (Phase 12): users consume directly from DataForge-hosted per-workspace Kafka topics with SASL/ACL credentials, plus HMAC-signed webhooks. Future: S3/Iceberg/CDC export to user-provided storage. `system-architecture.md` and `delivery-channels.md` must state this boundary explicitly.

## Load-bearing decisions baked into the specs (ADR-0001…0017)

1. **Monorepo**: `backend/` (Django+DRF), `frontend/` (Vite+React+TS), `infra/`, `specs/`.
2. **Tenancy**: shared schema + `workspace_id` everywhere + mandatory scoped managers + CI guard against unscoped models + Postgres RLS from day one (breach requires two simultaneous failures).
3. **Scenarios = declarative versioned manifests** run by one generic runtime; Python hooks only for value generation. This is what lets AI-generated scenarios slot in later with zero core changes.
4. **Canonical event envelope frozen first** (UUIDv7, `occurred_at` vs `emitted_at` with explicit clock-domain rules, `schema_ref`, `sequence_no`, `partition_key`, Debezium-shaped CDC `op`/`before`/`after`); additive-only after.
5. **Kafka is the internal backbone from day one** (single-node KRaft in compose); every delivery channel is a consumer adapter behind a `DeliveryChannel` interface — no substrate swap ever.
6. **Celery = control plane only**; generation runs in long-lived runner processes with Redis leases + heartbeats, reconciling desired state (running/paused/TPS/chaos) — avoids Celery tick-task throughput ceilings.
7. **Staged pipeline**: Behavior → clean ground-truth ledger → Chaos transform → Delivery. Chaos corrupts delivery truth, never business truth; every injection is recorded → instructor **answer-key API** (gradable exercises).
8. **State-first generation**: business events AND CDC events derive from the same entity-pool mutations → the two views are always consistent; CDC contract frozen at Phase 0, feature ships inside MVP.
9. **Deterministic seeds + virtual clock** (speed multiplier, backfill of N simulated days) → reproducible graded exercises and batch datasets for DBT/warehouse learners.
10. **Auth duality**: SimpleJWT for the console; opaque hashed workspace-scoped API keys (`df_<env>_<prefix>_<secret>`, shown once, Redis revocation cache) for the data plane.
11. **REST = replayable cursor over time-partitioned Postgres buffer (24–48h TTL, explicit cursor-expired error); WS = best-effort tail via Channels in a separate ASGI process group.**
12. **Fly.io**: one app, process groups sharing one image; managed Postgres/Redis; single-broker Kafka on a Fly volume with a pre-committed managed-Kafka migration trigger (external channel ships, >5k TPS, or SLO breach).

## Gaps the design panel found — specs must explicitly cover these

- AI/user-authored manifest **safety**: resource bounds, probability-sum + reachability validation, generator allowlist, DoS threat model (D6 + D14).
- **Late-arrival chaos buffer vs stream lifecycle**: semantics on pause/stop/resume/crash-failover (D8).
- **Timestamp clock domains** under virtual clock ("30 min late" = simulated or wall time?) — pinned in D5 before envelope freeze.
- **100k TPS capacity arithmetic** (per-runner ceiling, shards, partitions, buffer ingest) — not just an asserted target (D15).
- **99.9% availability**: honest SLO definition (control vs data plane) + post-GA path; single-region MVP can't meet it and the spec says so (D13/D15).
- S3/Iceberg/CDC export **contract-level** definitions now (file/commit semantics) so the sink interface isn't REST/WS-shaped (delivery-channels.md).
- Manifest **version pinning** for running streams; account lifecycle + signup abuse controls; cursor-expiry API contract; tenant-level Kafka topic/partition budgeting.

## Implementation phases (13 docs in `specs/07-plan/phases/`)

| Phase | Goal | Exit criteria (abridged) |
|---|---|---|
| 0 — Specs | All 20 deliverables; 6 one-way-door ADRs (0002/3/4/5/9/10) review-blocking, rest timeboxed | README maps D1–D20; e-commerce manifest validates on paper |
| 1 — Foundations | Monorepo scaffold, full compose stack (pg/redis/kafka/api/worker/runner/ws/web), CI, hello-world Fly deploy | `docker compose up` healthy; CI green; Fly serves healthz |
| 2 — Identity & tenancy | JWT auth, workspaces, hashed API keys, scoped managers + CI guard + RLS, audit log | cross-tenant attack suite passes; revoked key rejected <1s |
| 3 — Manifest + registry + envelope | Manifest schema v0 + validator, scenario catalog, schema registry, envelope | malformed manifests rejected precisely; zero e-commerce logic in Python |
| 4 — Generation core + batch | Entity pools, behavior engine v1, ground-truth ledger, batch JSONL datasets | golden-seed byte-identical replay; 1M-event referential-validity property tests |
| 5 — Streaming runtime | Runners (leases/failover), Kafka backbone, buffer-writer, cursor REST pull | kill-test failover <30s; cursor replay identical; isolation tests |
| 6 — Stream control | Pause/resume w/ checkpoints, dynamic TPS, WebSocket tail, stats | TPS 10→500 in <2s; 1-hr soak at 200 TPS stable |
| 7 — Console MVP | Full core flow in browser (all 7 page groups) | Playwright E2E of signup→stream→tail→stop in CI |
| 8 — Full e-commerce + CDC | 8 entities, full funnel, CDC c/u/d, intensity curves, backfill | 1M-event soak zero integrity violations; CDC consistency tests |
| 9 — Chaos engine | All 7 modes + answer-key API + exercise presets | statistical tolerance tests (5%±1% dupes); seed-identical injections |
| 10 — Schema evolution | v2/v3 schemas, mid-stream upgrade, registry UI | live v1→v2 upgrade without restart |
| 11 — Scale + obs + GA | Runner sharding, metrics/SLOs, quotas, prod Fly topology | ≥5k TPS load test + documented staircase to 100k; **MVP GA** |
| 12 — Delivery expansion (post-MVP) | External Kafka + webhooks through existing seam | diff confined to delivery adapters; zero engine changes |

MVP = Phases 0–11. Post-MVP: external Kafka/webhooks (12), S3/Iceberg/CDC export, AI scenario generation, more scenarios, 100k TPS substrate scaling.

## Execution approach (after plan approval)

1. **Author foundation docs first** (sequential dependencies): PRD → domain model → event model → manifest spec, since everything cites them.
2. **Fan out the rest via a Workflow** (ultracode): parallel authoring agents per spec group, each given the synthesis decisions + gap list above as binding constraints; then an **adversarial cross-doc consistency pass** (envelope ↔ DB schema ↔ API spec ↔ manifest example; chaos ↔ registry; phases ↔ ADRs) with fixes applied before presenting.
3. ADRs written in standard format (Context/Decision/Consequences/Status), the 6 one-way-door ADRs flagged as review-blocking.
4. Specs land as the first commits on `main` (empty repo), small logical commits per spec group.
5. **Then STOP** and present a review summary (per user decision: approval gate before any code).

## Verification

- `specs/README.md` traceability table: every deliverable D1–D20 maps to a file; no orphans.
- Cross-doc consistency review pass (step 2 above) — findings fixed, not just listed.
- E-commerce reference manifest validates by inspection against the manifest JSON Schema draft (the DSL expressiveness forcing function).
- Every phase doc has: goal, scope, non-goals, demo script, measurable exit criteria.
- The 10 panel-identified gaps each have a named section in a named doc (spot-checkable).
