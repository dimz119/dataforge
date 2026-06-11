# DataForge — Architecture Decision Records

**Deliverable:** D17

This directory is the decision log for DataForge: one file per architecturally significant decision, recording the forces, the decision itself, the alternatives the design panel considered, and the consequences. ADRs are the *why* behind the specs — every structural claim in [../03-domain/domain-model.md](../03-domain/domain-model.md), the engine docs, and the architecture docs cites an ADR by number. All seventeen decisions below were made by a three-perspective design panel plus adversarial critic synthesis and approved at Phase 0; the decisions are final for the MVP. Specs never relitigate them — alternatives live only here, in each ADR's "Alternatives considered" section.

---

## 1. Status legend

| Status | Meaning |
|---|---|
| **Proposed** | Drafted, under review; not yet binding. No ADR in this set holds this status — all seventeen were accepted at Phase 0. |
| **Accepted** | Binding. Implementation follows it; changing it requires a superseding ADR. |
| **Accepted — review-blocking (one-way door)** | Binding **and** a Phase 0 approval-gate item: the retrofit cost after implementation is prohibitive, so the user review of the specs must explicitly sign off on these six before any code is written ([../07-plan/phases/phase-00-specs.md](../07-plan/phases/phase-00-specs.md) exit criteria). |
| **Superseded by ADR-NNNN** | No longer current; kept verbatim for history. The superseding ADR explains what changed and why. |
| **Deprecated** | The decision's subject no longer exists in the system; no successor decision was needed. |

ADRs are immutable once Accepted: typo fixes are allowed, substantive changes are not. To change a decision, write a new ADR that names the one it supersedes (this is also the process the envelope evolution policy EV-6 in [../03-domain/event-model.md](../03-domain/event-model.md) §8 requires). Numbering is sequential and never reused.

## 2. Index

| ID | Title | Status | Review-blocking | Decision in one line |
|---|---|---|---|---|
| [ADR-0001](adr-0001-monorepo.md) | Monorepo layout | Accepted | No | One repo — `backend/`, `frontend/`, `infra/`, `specs/` — with one path-filtered CI pipeline; OpenAPI and manifest JSON Schema are CI artifacts so contracts and both apps evolve in lockstep. |
| [ADR-0002](adr-0002-multi-tenancy-model.md) | Multi-tenancy: shared schema + scoped managers + CI guard + RLS from day one | Accepted — review-blocking | **Yes** | One Postgres schema, non-null `workspace_id` everywhere, one enforcement chokepoint backed by a CI guard, plus Postgres RLS from Phase 2 as an independent second wall. |
| [ADR-0003](adr-0003-declarative-scenario-manifests.md) | Scenarios are declarative versioned manifests interpreted by a generic runtime | Accepted — review-blocking | **Yes** | A scenario is a validated, versioned manifest (entities, generators, state machines, preconditions, CDC, chaos defaults) run by one generic runtime; hooks are value-generation only; AI scenarios = manifests that pass validation. |
| [ADR-0004](adr-0004-canonical-event-envelope.md) | Canonical event envelope, frozen first, additive-only thereafter | Accepted — review-blocking | **Yes** | One envelope (UUIDv7, dual timestamps with clock-domain rules, `schema_ref`, `sequence_no`, `partition_key`, CDC `op`) frozen at Phase 0; internal labels stripped at the delivery boundary; additive-only evolution. |
| [ADR-0005](adr-0005-internal-kafka-backbone.md) | Internal Kafka backbone from day one; all delivery channels are consumer adapters | Accepted — review-blocking | **Yes** | Runners publish to internal Kafka topics; every channel — REST buffer, WS, later external Kafka/webhooks/S3-Iceberg — is a consumer behind the `DeliveryChannel` interface; no interim transport, ever. |
| [ADR-0006](adr-0006-celery-control-plane-runner-data-plane.md) | Celery is control plane only; leased runner processes are the data plane | Accepted | No | Celery handles lifecycle commands and jobs; generation runs in long-lived runner processes that hold Redis leases and reconcile control-plane desired state every tick. |
| [ADR-0007](adr-0007-behavior-engine-state-machines.md) | Behavior engine: per-actor state machines over entity pools | Accepted | No | Actors traverse manifest-defined state machines over shared entity pools with intensity curves; precondition guards make invalid sequences structurally impossible, not filtered. |
| [ADR-0008](adr-0008-deterministic-seeds-virtual-clock.md) | Deterministic per-stream seeds + virtual clock with speed multiplier and backfill | Accepted | No | Per-stream seed with namespaced sub-seeds ⇒ identical sequences regardless of wall pacing; per-stream virtual clock supports speed multipliers and N-day backfill datasets. |
| [ADR-0009](adr-0009-staged-pipeline-ground-truth-then-chaos.md) | Staged pipeline: Behavior → ground-truth ledger → Chaos → Delivery | Accepted — review-blocking | **Yes** | The behavior engine always produces a clean canonical stream persisted to a ledger; chaos is a seeded, ordered, composable transform after the ledger that corrupts delivery truth only, never business truth. |
| [ADR-0010](adr-0010-inhouse-schema-registry.md) | In-house schema registry: Postgres + JSON Schema, Confluent-compatible subjects | Accepted — review-blocking | **Yes** | Payload schemas live in Postgres as JSON Schema under `{scenario_slug}.{event_type}` subjects with enforced additive compatibility; drift chaos may only inject registered next-version fields. |
| [ADR-0011](adr-0011-auth-duality-jwt-and-api-keys.md) | Auth duality: JWT for the console, hashed workspace-scoped API keys for the data plane | Accepted | No | Humans get SimpleJWT access/refresh; machines get opaque `df_<env>_<prefix>_<secret>` keys, SHA-256-hashed, shown once, scoped, revocable within 1 s via a Redis revocation cache. |
| [ADR-0012](adr-0012-state-first-generation-cdc.md) | State-first generation: business events and CDC derive from the same entity-pool mutations | Accepted | No | CDC events (Debezium-shaped `op`/`before`/`after`) and business events are two projections of one pool mutation; CDC shape frozen Phase 0, emission ships in MVP (Phase 8). |
| [ADR-0013](adr-0013-rest-cursor-buffer-ws-tail.md) | REST = replayable cursor over a time-partitioned Postgres buffer; WS = best-effort tail | Accepted | No | A buffer-writer sink persists post-chaos events to a TTL'd Postgres buffer for at-least-once cursor replay; WebSocket is a live tail in a dedicated ASGI process, never the bulk path. |
| [ADR-0014](adr-0014-api-conventions.md) | API conventions: `/api/v1`, cursor pagination, RFC 9457 errors, OpenAPI as CI artifact | Accepted | No | URL-versioned REST, cursor pagination everywhere, problem-details errors, per-key rate limits; the frontend consumes a generated TypeScript client so contract drift fails the build. |
| [ADR-0015](adr-0015-flyio-process-groups-kafka-placement.md) | Fly.io: one app with process groups; single-broker KRaft Kafka with a pre-committed migration trigger | Accepted | No | One Fly app (web/ws/worker/runner), managed Postgres/Redis, internal-only single-broker Kafka on a volume; migration to managed Kafka triggers on the external channel shipping, ~5k sustained TPS, or SLO breach. |
| [ADR-0016](adr-0016-frontend-stack.md) | Frontend: Vite + React + TS SPA, TanStack Query, generated client, no Redux | Accepted | No | An authenticated SPA (no SSR) with TanStack Query for server state, a generated OpenAPI client, a dedicated WS hook layer with sampling, and in-memory access tokens with refresh rotation. |
| [ADR-0017](adr-0017-instructor-answer-key.md) | Instructor ground-truth answer-key API | Accepted | No | Every chaos injection and the canonical clean sequence are queryable by workspace admins via dedicated endpoints and a console panel; ground truth never appears in delivered payloads. |

Reading order for a new reviewer: 0002 → 0004 → 0003 → 0005 → 0009 → 0010 (the six one-way doors, in dependency order), then 0006/0007/0008/0012 (how generation works), then the rest.

## 3. ADR template

Every ADR in this directory follows this structure exactly:

```markdown
# ADR-NNNN — <Title: the decision as a noun phrase>

**Deliverable:** D17

<One paragraph: what is decided, in one or two sentences, and why this
decision warranted an ADR — i.e. what becomes expensive if it is wrong.>

- **Status:** Accepted | Accepted — review-blocking (one-way door) | Superseded by ADR-MMMM
- **Date:** YYYY-MM-DD
- **Decides for:** <phases/components bound by this decision>

## Context

<The forces: requirements cited verbatim or by spec reference, constraints,
scale numbers, failure modes, and the panel gap (if any) this closes.>

## Decision

<The full decision, expanded with specifics: exact mechanisms, names,
numbers, and boundaries. Cite the owning spec for implementation detail.>

## Alternatives considered

<Each alternative the panel held or that is commonly reached for, with the
concrete reason it was rejected — including which panel position (P1/P2/P3)
held it where applicable.>

## Consequences

### Positive
### Negative
### Follow-ups

<Follow-ups name the spec or phase that owns each open obligation.
"Refined in Phase N" markers are allowed; TODO/TBD are not.>
```

Conventions: terminology follows the ubiquitous language in [../03-domain/domain-model.md](../03-domain/domain-model.md) §6; invariants are cited by their stable `INV-*` IDs; cross-references use relative paths; an ADR is 60–150 lines — long enough to be unambiguous, short enough to be read at a review gate.
