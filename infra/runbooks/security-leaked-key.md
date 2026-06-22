# Security incident runbook: Leaked workspace API key

A workspace API key (the `df_<env>_<prefix>_<secret>` string, or its secret half) has
been exposed — committed to a public repo, pasted in a ticket/log, found by the gitleaks
`df_`-prefix rule, or reported by the customer. Revoke first, investigate second:
revocation is synchronous and platform-wide < 1 s (SEC-KEY-5), so containment is cheap.

**Severity:** sev-2 (sev-1 if the key is admin-scoped or `answer_key:read`-bearing, i.e.
it can read the chaos answer key — A-4).

## How revocation works (context — security §3.2.3, SEC-KEY-5/6/7)

- Every request verifies the key through the Redis revocation cache
  (`backend/tenancy/infra/revocation_cache.py`, key `apikey:state:{prefix}`) before any
  DB work. Active entries carry a **60 s** TTL; revoked entries are written
  **synchronously before the 204** on revoke with a **48 h** TTL.
- Revoke (`backend/tenancy/application/keys.py::revoke_key`) sets `revoked_at` in a DB
  transaction AND calls `revocation_cache.put_revoked(prefix)` **before** returning — so
  the key is rejected on the very next request, everywhere, in < 1 s.
- Fail-closed: if the synchronous Redis write fails, the DB truth still commits, a
  `tenancy.api_key.revocation_cache_degraded` audit event is emitted, and the worst-case
  staleness degrades to the 60 s active-TTL bound — never to "allow" (SEC-KEY-7).

## Detect

| Signal | Source |
|---|---|
| gitleaks `df_`-prefix hit | pre-commit hook / CI gitleaks stage (`infra/ci/gitleaks.toml`) |
| Customer/operator report | support channel |
| Anomalous usage on one key | `df_rate_limited_total{scope}`, audit `tenancy.api_key.*`, `last_used_at` |

1. Identify the key by its **public prefix** (`df_<env>_<prefix>` — the non-secret durable
   handle; the secret/hash is never logged, INV-AUD-3). The `GET /workspaces/{id}/api-keys`
   list shows each key by `prefix` + `last4` only.

## Revoke (containment — do this first)

1. Revoke the single key (key creator or any workspace admin; api-spec #24):
   `DELETE /workspaces/{ws}/api-keys/{key_id}` → 204. This runs `revoke_key`: DB
   `revoked_at` + synchronous `put_revoked` before the response.
2. If the whole workspace's keys are suspect (broad leak), revoke them all —
   `revoke_all_workspace_keys` (`keys.py`; SEC-KEY-8) walks every active key, revoking +
   caching each.
3. Mint a replacement for the customer if needed: `POST /workspaces/{ws}/api-keys`
   (reveal-once 201). The new key has a fresh prefix; the leaked prefix stays dead 48 h
   in cache and forever in the DB.

## Confirm rejection < 1 s

1. Present the leaked key against any authenticated route — expect **401 `invalid-api-key`**
   (the single closed slug; no state oracle, A-3). It should fail on the first request
   after the 204.
2. Confirm the cache carries the revoked state for the prefix:
   `apikey:state:<prefix>` in Redis holds `{"state":"revoked"}` (TTL ≤ 48 h).
3. If the revoke returned 204 but the key still authenticates, the synchronous cache write
   degraded — check the audit log for `tenancy.api_key.revocation_cache_degraded` and Redis
   health; the key will still hard-fail within 60 s (active-TTL expiry → DB `revoked_at`
   wins). Stabilize Redis ([restart-component.md](restart-component.md)) so the < 1 s
   contract holds again.

## Audit

1. Review the key's history in the workspace audit log:
   `GET /workspaces/{ws}/audit-log?action_prefix=tenancy.api_key.` — look for
   `tenancy.api_key.created` (who/when/scopes) and `tenancy.api_key.revoked`.
2. Review `last_used_at` (write-behind, SEC-KEY-9, flushed at minute precision) to bound
   the exposure window — when the leaked key was last seen, and whether usage continued
   after the suspected leak time.
3. Scope-check: if the leaked key held `answer_key:read` (ADMIN_ONLY_SCOPES), treat as
   sev-1 — the chaos answer key may have been read; capture evidence per
   [security-cross-tenant-suspected.md](security-cross-tenant-suspected.md) before any
   remediation that mutates state.

## Notify + rotate

1. Notify the workspace owner/admins: the key was revoked, why, the exposure window from
   `last_used_at`, and the replacement key (delivered reveal-once).
2. If the leak was in **our** surface (a log line, a committed file), remediate the source
   (purge the secret, add a gitleaks rule if a new shape, scrub logs) — the secret-redaction
   layer (`backend/config/logging.py`) should already mask it; confirm.
3. Rotate any **platform** secret only if a platform credential (not a workspace key) was
   involved → [security-key-rotation.md](security-key-rotation.md).

## Verification

- The leaked prefix returns 401 on every surface (REST + WS); no 2xx carrying its data.
- `tenancy.api_key.revoked` (and `created` for the replacement) present in the audit log.
- `last_used_at` for the leaked key stops advancing after the revoke.
- Workspace notified; if platform-secret involvement, the rotation runbook ran to green.
