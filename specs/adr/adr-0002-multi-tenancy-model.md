# ADR-0002 — Multi-tenancy: shared schema + scoped managers + CI guard + RLS from day one

**Deliverable:** D17

DataForge isolates tenants in one shared Postgres schema with a non-null `workspace_id` on every tenant-owned table, enforced at a single application chokepoint, verified by a CI guard, and independently backed by Postgres Row-Level Security from Phase 2. This is a one-way door: the tenancy model determines every table, every query, every Kafka key, and every Redis key the platform will ever write — migrating between tenancy models after data exists is a full-platform rewrite, and a cross-tenant leak is the single most catastrophic failure DataForge can have.

- **Status:** Accepted — review-blocking (one-way door)
- **Date:** 2026-06-10
- **Decides for:** all persistence, all APIs, the envelope, Kafka key-space, Redis key-space; implemented Phase 2, binding forever

## Context

The forces:

- **The requirement is absolute:** "strict tenant/workspace/API-key/event-ownership isolation … No cross-workspace data access ever." The PRD's counter-metric for trust is *zero* referential or isolation violations, ever (PRD §8). The workspace is the tenant — a user account is not (domain model §6.1, INV-TEN-1).
- **Tenant shape:** thousands of small workspaces — free personal workspaces and classroom cohorts of up to 60 members (PRD §7) — created self-serve at signup. Workspace creation must be an `INSERT`, not an operational event.
- **The boundary spans both planes.** Isolation is not only Postgres rows: event envelopes, internal Kafka partition keys, Redis hot state, the event buffer, stream stats, and audit entries all carry tenant ownership (INV-TEN-1, INV-G-1). A tenancy model that only thinks in tables under-specifies half the system.
- **Django's defaults are unsafe here.** Default model managers are unscoped; one forgotten `.objects.filter(...)` without a workspace predicate is a silent leak. Convention without enforcement fails exactly once, catastrophically.
- **Failure-mode asymmetry:** every other defect in DataForge is recoverable; a cross-tenant leak in a classroom SaaS is reputation-terminal. The design target is that a breach requires at least two simultaneous, independent control failures.

## Decision

One Postgres schema; **defense in depth with two independent walls plus a build-time guard**:

1. **Shared schema, mandatory column.** Every tenant-owned table carries a non-null `workspace_id` (UUID, FK to `workspaces`). No schema-per-tenant, no database-per-tenant. Composite indexes lead with `workspace_id`; DDL and index catalog owned by [../03-domain/database-schema.md](../03-domain/database-schema.md).
2. **Wall 1 — one application chokepoint.** A request-bound workspace-context middleware resolves the acting workspace (from the URL scope for JWT console requests, from the key's binding for API-key requests per ADR-0011) and binds it request-locally. All tenant models use a mandatory `WorkspaceScoped` model-manager base whose querysets always filter by the bound workspace; all tenant viewsets derive from a workspace-scoped base. There is exactly one place isolation logic lives; nothing tenant-owned is reachable around it.
3. **CI guard.** A static check fails the build if any model with a `workspace_id` field does not use the scoped manager base, or any viewset over a tenant model bypasses the scoped base. Phase 2's exit criteria include demonstrating the guard fails on a deliberately planted unscoped model.
4. **Wall 2 — Postgres RLS, from Phase 2, not later.** Every tenant table gets `ENABLE ROW LEVEL SECURITY` with a policy of the form `USING (workspace_id = current_setting('app.workspace_id')::uuid)`; the application sets the session variable per transaction (middleware for requests; explicit context managers for Celery tasks and runner processes). RLS is verified independently in the cross-tenant attack suite with the ORM bypassed. Wall 2 catches what Wall 1 misses, and vice versa — a leak requires both to fail at once (INV-G-1).
5. **Data plane carries the tenant too.** `workspace_id` is a mandatory envelope field (event-model §2.1 field 3) and the mandatory first segment of every Kafka `partition_key` (event-model §2.2.3), so tenant attribution is inspectable at the broker. Redis keys for tenant state, buffer rows, ledger rows, stats counters, and audit entries are all workspace-scoped (INV-TEN-1).
6. **Permanent verification.** The cross-tenant attack suite — every endpoint probed with foreign-workspace credentials expecting 403/404, plus ORM-bypassed RLS probes — is a permanent CI gate from Phase 2 ([../06-quality/testing-strategy.md](../06-quality/testing-strategy.md)). The full enforcement-stack design is owned by [../06-quality/security-architecture.md](../06-quality/security-architecture.md).

The stack at a glance:

| Layer | Mechanism | Catches | Verified by |
|---|---|---|---|
| Schema | Non-null `workspace_id` on every tenant table | Untagged rows existing at all | Migration review; DDL in database-schema.md |
| Wall 1 (runtime) | Workspace-context middleware + mandatory `WorkspaceScoped` manager/viewset bases | Unscoped queries at request time | Cross-tenant attack suite (403/404 semantics) |
| Build guard | CI check: tenant model/view not on the scoped bases fails the build | The forgotten-filter class of bug, before merge | Planted-violation test (Phase 2 exit criterion) |
| Wall 2 (database) | Postgres RLS policies keyed on `app.workspace_id` GUC | Anything that defeats or bypasses Wall 1, incl. raw SQL | ORM-bypassed RLS probes in the attack suite |
| Data plane | `workspace_id` in envelope, partition-key prefix, Redis key prefixes | Cross-tenant leakage outside Postgres | Per-channel isolation tests; max-rate chaos isolation test (Phase 9) |

## Alternatives considered

- **Schema-per-tenant** (e.g. django-tenants). Strong default isolation, but rejected: at thousands of self-serve workspaces, every migration fans out across thousands of schemas (deploy time scales with tenant count); workspace creation becomes DDL; `search_path` management must be threaded through Celery workers, runner processes, and Kafka consumers — three non-request execution contexts where it is easy to get wrong; and it does nothing for the non-Postgres half of the boundary (Kafka, Redis, buffer). The model fits tens of large tenants, not thousands of classroom-sized ones.
- **Database-per-tenant.** Maximal isolation, rejected outright: connection-pool exhaustion and per-database cost are prohibitive at a free tier measured in thousands of workspaces; operationally it is schema-per-tenant's problems squared.
- **Shared schema with application scoping only; RLS deferred as "later hardening"** — panel position P1. Rejected per the resolved panel disagreement: on a greenfield schema RLS costs almost nothing at Phase 2 (the policies are written once, against tables that are being created anyway), whereas "deferred hardening" on the single most catastrophic failure mode is when it never happens. P2/P3's position won.
- **RLS only, no application chokepoint.** Rejected: RLS failures surface as silently empty querysets or low-level permission errors deep in a request — the wrong API semantics (the cross-tenant policy requires deliberate 403/404, security architecture). The application chokepoint provides correct semantics and composable querysets; RLS provides the independent guarantee. Each wall alone is a single point of failure; together they are cheap.

## Consequences

### Positive

- Workspace creation is one row; migrations run once; the operational model is boring at any tenant count.
- A leak requires two simultaneous independent failures, and the claim is *continuously tested* — the attack suite and the planted-violation guard make isolation a regression-proof property rather than a launch-time audit.
- Broker-level and Redis-level attribution (workspace-prefixed keys) extends the same model across the data plane with no second mechanism.

### Negative

- Every tenant query carries the workspace predicate and the session GUC must be set on every code path that touches tenant tables — including Celery tasks and runners, where forgetting it yields empty reads (fail-closed, but a debugging cost). The context-manager discipline is specified in [../02-architecture/backend-architecture.md](../02-architecture/backend-architecture.md).
- RLS adds a small per-query planner/filter overhead; accepted, and bounded by the `workspace_id`-leading indexes.
- Shared infrastructure means noisy-neighbor pressure is handled by quotas (PRD §7, INV-TEN-5) rather than physical separation — the quota system is therefore part of the isolation story, not an optional nicety.

### Follow-ups

- [../03-domain/database-schema.md](../03-domain/database-schema.md): RLS policy DDL for every tenant table; index catalog.
- [../06-quality/security-architecture.md](../06-quality/security-architecture.md): middleware/manager design, GUC discipline, cross-tenant policy (403 vs 404).
- [../06-quality/testing-strategy.md](../06-quality/testing-strategy.md): attack-suite contents as a permanent CI gate; Phase 2 exit criteria bind to it.
