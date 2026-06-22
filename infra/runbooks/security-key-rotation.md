# Security incident runbook: Platform secret rotation (SEC-TLS-3)

Routine (90-day cadence) or forced (suspected exposure) rotation of a **platform** secret:
`DJANGO_SECRET_KEY`, `JWT_SIGNING_KEY`, the Postgres credentials (`DATABASE_URL` /
`MIGRATE_DATABASE_URL`), or the Kafka credentials. All platform secrets live **only** in
`fly secrets` (rule S-1/S-2, deployment-architecture §5); none are committed — `fly.toml`
/ `fly.prod.toml` / `fly.kafka.toml` carry public `[env]` config only.

> Scope note: workspace **API keys** are not platform secrets — for a leaked customer key
> use [security-leaked-key.md](security-leaked-key.md). This runbook is for the platform's
> own credentials.

## Secret inventory (names only; values never leave `fly secrets`)

| Secret | Used by | Rotation class |
|---|---|---|
| `DJANGO_SECRET_KEY` | all process groups (Django) | rolling restart |
| `JWT_SIGNING_KEY` | `web` (mint/verify access+refresh) | **break-glass — invalidates all tokens** |
| `DATABASE_URL` (NOBYPASSRLS `dataforge_app`) | `web`/`ws`/`worker`/`runner` | two-credential swap |
| `MIGRATE_DATABASE_URL` (owner) | release `migrate`/`provision` | two-credential swap |
| Kafka creds / `CLUSTER_ID` | `runner`, `buffer-writer`, kafka app | rolling, per process group |

`JWT_SIGNING_KEY` is a **dedicated** 256-bit key, never `DJANGO_SECRET_KEY`
(security §3.1.2; `backend/config/settings/base.py` `SIMPLE_JWT["SIGNING_KEY"]`).

## General safe rolling rotation (per process group)

1. Set the new value: `fly secrets set <NAME>=<new> -a $FLY_APP`. Fly stages it and
   triggers a rolling restart; group order `worker → runner → ws → web` (control plane
   first — same order as [deploy-rollback.md](deploy-rollback.md) RB-1).
2. For a value the app can read at boot only (e.g. `DJANGO_SECRET_KEY`): the rolling
   restart picks it up group-by-group; sessions/CSRF tokens signed with the old key are
   invalidated as each group cycles — acceptable, users re-auth on the next request.
3. **Verify:** `readyz` healthy for every group; 5xx (`df_http_requests_total{status=~"5.."}`)
   and consumer-lag dashboards quiet 15 min; one `infra/scripts/prod-smoke.sh` pass.

## `JWT_SIGNING_KEY` rotation (break-glass — read this first)

Rotating the signing key **invalidates every outstanding access AND refresh token** — there
is no dual-key verification by design (SEC-TLS-3): the ≤ 15-min access-token lifetime bounds
the disruption, and refresh tokens minted under the old key fail verification, so **every
user must re-login**. This is the documented trade-off (MVP does not build dual-key).

1. Announce a brief forced-logout window to customers (all sessions end).
2. `fly secrets set JWT_SIGNING_KEY=<new-256-bit> -a $FLY_APP` (generate with a CSPRNG; see
   `infra/fly/README.md`). Rolling restart cycles `web`/`ws`.
3. **Refresh-token-family implication:** old refresh tokens no longer verify, so the
   rotation/reuse machinery (`backend/identity/application/auth.py`,
   BLACKLIST_AFTER_ROTATION + family revocation, SEC-AUTH-9) is moot for pre-rotation
   tokens — they are rejected at signature check, never reaching reuse detection. The
   SimpleJWT `BlacklistedToken`/`OutstandingToken` rows for old tokens are now dead weight;
   they age out naturally and need no manual purge.
4. **Verify:** a pre-rotation access token → 401 `authentication-required`; a fresh login
   issues a working pair; the refresh→rotate path mints a new pair under the new key
   (`identity/tests/test_jwt_auth.py` is the behavior reference).

## Database credential rotation (two-credential swap)

The managed Postgres provider supports two live credentials; rotate without downtime:

1. Provision/enable the second credential at the provider for the same role
   (`dataforge_app` stays NOSUPERUSER NOBYPASSRLS — never rotate it into a bypassing role;
   `backend/tenancy/management/commands/provision_db_roles.py` is the role contract).
2. `fly secrets set DATABASE_URL=<new-dsn> -a $FLY_APP` → rolling restart in group order.
   Rotate `MIGRATE_DATABASE_URL` (owner role) the same way if its credential changed.
3. After every group is on the new DSN and healthy, retire the old credential at the
   provider.
4. **Verify:** all groups `readyz`; the CI isolation lane's guarantee still holds in prod —
   `dataforge_app` is non-owner, no `BYPASSRLS` (the raw-SQL RLS probes prove RLS bites,
   [security-cross-tenant-suspected.md](security-cross-tenant-suspected.md)). No 5xx burst.

## Kafka credential rotation

Per the managed broker's standard swap; rotate `runner` + `buffer-writer` secrets, then the
kafka app, restarting each group rolling. The data plane tolerates a runner restart with no
canonical gap (per-shard `sequence_no` gapless, INV-GEN-5) — see [restart-runner.md](restart-runner.md)
and [restart-buffer-writer.md](restart-buffer-writer.md). Verify consumer lag recovers and
the per-shard sequence is gapless across the restart.

## Verification (any rotation)

- The old secret no longer works (old token 401 / old DSN refused at provider / old key dead).
- All process groups `readyz` healthy; 5xx + consumer-lag dashboards quiet 15 min; prod-smoke
  passes; zero new ERROR lines.
- The new secret's age clock resets — record the rotation date for the ≤ 90-day inventory
  reconciliation (GA-SECURITY-CHECKLIST item 9). No secret value ever appeared in a log,
  ticket, or commit.
