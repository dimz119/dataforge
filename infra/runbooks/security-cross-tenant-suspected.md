# Security incident runbook: Suspected cross-tenant data exposure (sev-1)

A workspace may have seen another workspace's data — the single most serious incident class:
a tenant-isolation breach. Isolation is the platform's load-bearing durability invariant
(INV-TEN-*), so this is **sev-1** and a **release-blocker class per the error-budget
policy**: a confirmed cross-tenant leak halts releases until root-caused and re-verified.

**Capture evidence BEFORE any restart or mutation** (security §13.2 item 15) — a restart can
erase the in-memory/request state that proves how the leak happened.

## Triggers

- A customer reports seeing data they don't own.
- A cross-tenant probe failure in CI/staging (the suite below) — treat as a real breach until
  disproven.
- An anomaly: a 2xx response carrying another workspace's ids, an `A-sentinel` in a B
  response, or a `permission-denied` (403) on a *foreign* object (403 confirms existence and
  is itself a masking failure — W-3 requires 404).

## The 3-layer isolation model (what should make this impossible)

DataForge enforces tenant isolation in three independent layers; a leak means a hole opened
in all three at once for the affected path. Verification re-proves each:

1. **Application — scoped managers + 404 masking.** Every tenant model uses a workspace-scoped
   default manager; foreign objects return **404**, never 403 (existence is never confirmed).
2. **Database — Postgres RLS, FORCE'd, under a NOBYPASSRLS role.** The runtime `dataforge_app`
   role is `NOSUPERUSER NOBYPASSRLS`; RLS policies key on the `app.workspace_id` GUC, so even
   raw SQL sees zero foreign / zero unset-GUC rows.
3. **Strip boundary — the `_df` block never leaves the engine.** Delivered envelopes are
   exactly the 20-key `DELIVERED_FIELD_SET`; the SB-3 scan proves no `_df`-prefixed internal
   key (which could carry foreign routing/answer-key data) escapes at any nesting level.

## Immediate containment (before evidence loss)

1. **Capture evidence first.** Snapshot, do not restart:
   - the exact request/response that leaked (headers minus secrets, body, `X-Request-ID`);
   - the structured logs for that request id (`backend/config/logging.py` — secrets are
     redaction-masked, so logs are safe to retain);
   - the relevant audit-log rows: `GET /workspaces/{ws}/audit-log` for both the source and
     victim workspaces (the audit log is append-only — `dataforge_app` has no UPDATE/DELETE
     on `audit_log`, so it is tamper-evident evidence).
2. **Contain the affected surface.** If a specific route is leaking, take it out of rotation
   (feature-flag/edge block) rather than restarting the fleet — preserve state.
3. If the leak is via a credential (a key that can read across workspaces), revoke it →
   [security-leaked-key.md](security-leaked-key.md). Capture its `last_used_at` and audit
   history first.

## 3-layer RLS verification (prove which layer failed — or that none did)

Run the permanent isolation suites against the suspect environment (compose/staging Postgres;
RLS is a Postgres construct and these run in the CI **isolation lane** under the non-owner
`dataforge_app` role — `.github/workflows/ci.yaml`):

1. **Cross-tenant attack probe (TEN §7.2):** `backend/tests/tenancy/test_cross_tenant_probes.py`
   — every `/api/v1` route × {foreign JWT, foreign API key, no credential}, asserting
   object→404, sub-collection→404, no 2xx carrying foreign data, no 5xx, no `permission-denied`
   on a foreign object, no A-sentinel in any body. Routes are enumerated at collection time,
   so a new endpoint is probed by construction.
2. **Raw-SQL RLS probes (TEN §7.3):** `backend/tests/tenancy/test_rls_raw_sql.py` — opens a
   raw cursor as the application role, sets `app.workspace_id`/`app.user_id` to workspace B,
   and asserts `SELECT count(*) … WHERE workspace_id = <A>` returns **0** for every
   RLS-bearing table (foreign GUC and unset GUC both → zero rows). The table list is derived
   from migration state (every `EnableRowLevelSecurity` op), so a new tenant table without RLS
   is caught.
3. **`_df` strip scan (SB-3):** `backend/tests/delivery/test_sb3_strip_scan.py` — channel-
   parameterized deep scan over every shipped channel's delivered output; no `_df` at any
   nesting level. Run against the real partitioned buffer.
4. Confirm the role contract in the suspect env: `dataforge_app` is non-owner, `NOBYPASSRLS`,
   no `UPDATE/DELETE` on `audit_log`/`workspace_quotas`
   (`backend/tenancy/management/commands/provision_db_roles.py`). A role drifted into
   `BYPASSRLS` would silently disable layer 2 — check this explicitly.

A suite that **passes** while a real leak occurred means the breach is on a path the probes do
not cover (a new route not classified in `access_policy`, a manager bypass, a strip-boundary
miss) — add the probe to close the gap by construction.

## Audit-trail review

- Read both workspaces' audit logs (append-only; `backend/audit/`) around the incident window
  for `tenancy.api_key.*`, `identity.auth.*`, and any control-plane action that touched the
  victim's resources.
- Correlate by `X-Request-ID`; the audit writer scrubs secret-shaped keys
  (`backend/audit/infra/sanitize.py`) so the trail is safe to export as evidence.

## Post-mortem trigger (release-blocker)

1. A **confirmed** cross-tenant exposure is a sev-1 / release-blocker per the error-budget
   policy: freeze releases for the affected surface until root cause + fix + a new regression
   probe are merged and the three suites above are green.
2. Mandatory post-mortem: which layer(s) failed and why all three did not catch it; the new
   probe that makes this exact leak impossible by construction; customer notification per the
   data-exposure policy.
3. Only after the regression probe is committed and green does the release freeze lift.

## Verification (incident closed)

- The cross-tenant probe, raw-SQL RLS probes, and SB-3 strip scan are all green against the
  fixed code in the isolation lane.
- A new regression test reproduces the original leak and now fails-closed.
- `dataforge_app` confirmed non-owner / `NOBYPASSRLS` / no U-D on `audit_log` in every env.
- Evidence (request/response, logs, audit rows) archived; post-mortem filed; release freeze
  lifted only after the regression probe is committed.
