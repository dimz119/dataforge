# Phase 2 — Identity, Tenancy, API Keys, Audit

**Deliverable:** D18 (phase doc)

Phase 2 builds the tenant boundary before anything worth stealing exists: real users, workspaces, hashed API keys, the three-layer isolation stack (scoped managers + CI guard + Postgres RLS, ADR-0002), and the append-only audit log. The cross-tenant attack suite and the tenancy CI guard land here and run on every PR forever — isolation is proven adversarially from this phase onward, never assumed.

## Goal

> The tenant boundary is real and provably leak-proof before any domain feature exists.

## Dependencies

| Dependency | Role |
|---|---|
| Phase 1 complete | Booting stack, CI pipeline, Postgres/Redis available |
| [../../06-quality/security-architecture.md](../../06-quality/security-architecture.md) | Auth duality (SEC-AUTH-*), key lifecycle (SEC-KEY-*), account lifecycle (SEC-ACC-*), enforcement stack, abuse controls — the spec this phase implements |
| [../../03-domain/domain-model.md](../../03-domain/domain-model.md) §2.1, §2.2, §2.10, §5 | Identity/Tenancy/Audit aggregates, INV-ID-*/INV-TEN-*/INV-AUD-*, API-key lifecycle |
| [../../03-domain/database-schema.md](../../03-domain/database-schema.md) | DDL for `users`, `workspaces`, `memberships`, `api_keys`, `audit_log`, `quotas`; RLS policies |
| [../../05-interfaces/api-specification.md](../../05-interfaces/api-specification.md) | Endpoint shapes, RFC 9457 problem types, 401/403/404 policy |
| [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §7 | TEN suite design (probes, RLS verification, guard canaries) |
| ADR-0002 (tenancy), ADR-0011 (auth duality) | Structural decisions implemented here |

## Scope

- **Identity:** custom user model live; `POST /api/v1/auth/signup` (creates `is_verified = false`, sends verification email — Mailpit in dev), `verify-email` (single-use token, TTL 24 h), `login` (SimpleJWT: 15-min access + rotating `df_refresh` HttpOnly cookie, 7 d), `refresh`, password reset (single-use token, TTL 1 h). These are real flows, not stubs. INV-ID-2 enforced: unverified users cannot create workspaces, accept invitations, or create API keys.
- **Tenancy:** Workspace + Membership models with roles `admin`/`member`; workspace CRUD + member invitation/role-change/removal APIs; sole-admin rule (INV-TEN-3); quota rows seeded with Free-tier defaults (PRD §7) — *enforcement metering is Phase 11*.
- **API keys (ADR-0011):** format `df_<env>_<prefix>_<secret>`; SHA-256 hash + prefix + last4 stored, plaintext in the creation response only (INV-TEN-4); scopes from the domain-model §2.2 vocabulary; revoke by creator or admin, effective < 1 s via the Redis revocation cache; `last_used_at` write-behind; environment-token check (`df_dev_*` rejected in prod, SEC-KEY-2). A key-introspection endpoint (`GET /api/v1/auth/key-info`, returns workspace, prefix, scopes for the presented key) ships as the data-plane auth probe target until Phase 5 endpoints exist.
- **Enforcement stack (the two-walls claim):** workspace-context middleware (fail-closed contextvar lifecycle per backend-architecture §6), mandatory `WorkspaceScoped` manager + scoped viewset base for every tenant model, the `check_tenancy` CI guard command failing on any unscoped tenant model/manager/viewset, and **Postgres RLS policies on every tenant table** (default-deny when the workspace GUC is unset).
- **Audit:** append-only `audit_log` with the domain-model §2.10 minimum action set, written in the same transaction as the mutation (INV-AUD-2); admin-readable per workspace via `GET /api/v1/workspaces/{id}/audit-log`.
- **Abuse controls:** per-IP signup rate limits, disposable-email denylist (SEC-ACC-9), captcha hook with provider `none|turnstile` shipped but defaulting to `none` (SEC-ACC-10).
- **Permanent CI gates land:** TEN attack suite (OpenAPI route auto-enrollment, two-workspace fixture, raw-SQL RLS probes) and GUARD tenancy canaries, both unskippable from this phase forever.

## Non-goals

| Deferred | Lands in |
|---|---|
| Scenarios, streams, events, any data-plane resource | Phases 3–5 |
| Quota *metering* (events/day counters, idle auto-pause) — only command-time caps and seeded quota rows exist | Phase 11 |
| `answer_key:read` scope is grantable, but no answer-key endpoint exists | Phase 9 |
| Console auth/workspace/key pages | Phase 7 |
| Account-deletion grace-period jobs beyond the documented state machine (`pending_deletion` flow per security-architecture §5) | Implemented here; billing interactions Phase 11 |
| Per-key rate-limit tiers beyond the basic limiter | Phase 11 |

## Tasks

- [ ] Custom user model + migrations; email normalization (INV-ID-1); soft-delete tombstone
- [ ] Signup + verification-email flow (Mailpit in dev); single-use 24 h tokens (INV-ID-3); resend endpoint with rate limit
- [ ] SimpleJWT login/refresh: 15-min access, rotating `df_refresh` cookie per SEC-AUTH-3
- [ ] Password-reset flow: request + confirm, single-use 1 h tokens, no reset for unverified accounts (SEC-ACC-8)
- [ ] Account deletion flow with sole-admin guard (INV-ID-4 / INV-TEN-3)
- [ ] Workspace + Membership models, roles, CRUD + invitation APIs; INV-TEN-2/3 unit tests
- [ ] Quota table seeded with Free-tier defaults per PRD §7
- [ ] API-key model + create/revoke endpoints: hash/prefix/last4, reveal-once, scopes; key-info introspection endpoint
- [ ] Redis revocation cache + `ApiKeyAuthentication` DRF class (hash check → revocation check → env-token check)
- [ ] Workspace-context middleware + `WorkspaceScoped` manager + scoped viewset base
- [ ] `check_tenancy` CI guard command + GUARD canary meta-tests (testing-strategy §7.4)
- [ ] RLS policies on all tenant tables, default-deny; migration-derived tenant-table enumeration
- [ ] Audit log model + same-transaction writer + admin read API; secret-free assertion tests (INV-AUD-3)
- [ ] Signup abuse controls: per-IP limits, disposable-email denylist, captcha hook (default off)
- [ ] TEN suite: two-workspace fixture, OpenAPI route enumerator + access-policy table, JWT/key/no-cred probes
- [ ] TEN RLS probes via raw psycopg (ORM bypassed) per testing-strategy §7.3
- [ ] Demo walkthrough script `infra/scripts/demo-phase02.sh` (the curl sequence below, reused by later phase demos)

## Demo script

1. `docker compose -f infra/compose/compose.yaml up -d --wait`
2. Signup: `curl -s -X POST localhost:8000/api/v1/auth/signup -H 'Content-Type: application/json' -d '{"email":"ada@example.com","password":"correct-horse-battery"}'` → `201`.
3. Fetch the verification token from Mailpit (`curl -s localhost:8025/api/v1/messages | jq`), then `curl -s -X POST localhost:8000/api/v1/auth/verify-email -d '{"token":"<token>"}'` → `200`.
4. Login: `ACCESS=$(curl -s -X POST localhost:8000/api/v1/auth/login -d '{"email":"ada@example.com","password":"correct-horse-battery"}' | jq -r .access)` — response also sets the `df_refresh` cookie; the body never contains it.
5. Create workspace: `WS=$(curl -s -X POST localhost:8000/api/v1/workspaces -H "Authorization: Bearer $ACCESS" -d '{"name":"Ada Lab"}' | jq -r .workspace_id)`.
6. Create key: `RESP=$(curl -s -X POST localhost:8000/api/v1/workspaces/$WS/api-keys -H "Authorization: Bearer $ACCESS" -d '{"name":"demo","scopes":["events:read","streams:write"]}')`; `KEY=$(echo $RESP | jq -r .key)`; `KEY_ID=$(echo $RESP | jq -r .api_key_id)` — plaintext `df_dev_…` shown once; `GET …/api-keys` afterwards shows only `prefix…last4`.
7. Use the key: `curl -s localhost:8000/api/v1/auth/key-info -H "X-API-Key: $KEY" | jq .workspace_id` → `$WS` (API keys ride `X-API-Key`, never `Authorization` — api-spec §2.2/A-2).
8. Revoke and time it: `curl -s -o /dev/null -w '%{http_code}' -X DELETE localhost:8000/api/v1/workspaces/$WS/api-keys/$KEY_ID -H "Authorization: Bearer $ACCESS"` → `204`; `sleep 1; curl -s -o /dev/null -w '%{http_code}' localhost:8000/api/v1/auth/key-info -H "X-API-Key: $KEY"` → `401` (within 1 s of revocation, api-spec #24).
9. Cross-tenant probe: repeat steps 2–6 as `bob@example.com` → workspace B; `curl -s -o /dev/null -w '%{http_code}' localhost:8000/api/v1/workspaces/$WS -H "Authorization: Bearer $ACCESS_BOB"` → `404`.
10. Guard demo: `pytest backend/tests/guards/test_tenancy_guard.py -q` — plants an unscoped canary model and asserts `check_tenancy` exits non-zero naming it; corrected canaries pass.
11. RLS demo: `pytest backend/tests/tenancy/test_rls_raw_sql.py -q` — raw psycopg under workspace-B GUC selects 0 of A's rows; unset GUC selects 0 rows.
12. Audit: `curl -s localhost:8000/api/v1/workspaces/$WS/audit-log -H "Authorization: Bearer $ACCESS" | jq '.data[].action'` — shows the workspace-scoped actions `tenancy.workspace.created`, `tenancy.api_key.created`, `tenancy.api_key.revoked`. Account-level events such as `identity.user.registered` are written with a NULL `workspace_id` and are deliberately **excluded** from the per-workspace audit-log read (INV-AUD-4 / security §10.4) — visible only to the account owner via operator tooling.

## Exit criteria

Binding text with measurable assertions; proving suites per [../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md) §14 (Phase 2 rows).

| # | Binding criterion | Measurable assertion | Proving suite (lane) |
|---|---|---|---|
| 1 | "curl demo: signup → workspace → issue key → revoke" | Demo steps 2–8 complete as written, scripted in `demo-phase02.sh`, rerunnable from a clean stack | OPS scripted API walkthrough (merge) |
| 2 | "cross-tenant attack suite passes (every endpoint probed with foreign-workspace credentials returns 403/404; RLS verified independently with ORM bypassed)" | Every OpenAPI route classified in the access-policy table and probed with foreign JWT + key + no-cred variants → 404/403/401 per policy, zero 2xx/5xx, zero sentinel leakage; raw-SQL probes return 0 rows under foreign and unset GUC for every tenant table | TEN §7.2 + §7.3 (PR, permanent, unskippable) |
| 3 | "CI guard demonstrably fails on a planted unscoped model" | All three canary classes (no `workspace_id`, default manager, unscoped viewset) make `check_tenancy` exit non-zero naming the offender; corrected controls pass | GUARD §7.4 meta-tests (PR, permanent) |
| 4 | "revoked key rejected within 1 s" | Data-plane request with a key revoked ≥ 1 s earlier returns 401, measured by stopwatch assertion against the Redis revocation cache | OPS-6 (merge) |
