# DataForge — production deploy artifacts (P11-10)

Reviewable Fly.io deploy documentation and configs for the ADR-0015 topology
(`deployment-architecture.md` §3–§8). **Phase 11 scope is artifact-only**: these
files are reviewed and parse-validated, *not* executed against real Fly — the
live prod deploy and the on-prod ≥5k/30-min GA gate are skipped this phase
(USER-DECIDED SCOPE). The eventual deploy is then a no-surprise `flyctl deploy`.

| Artifact | Purpose |
|---|---|
| [`../fly/fly.prod.toml`](../fly/fly.prod.toml) | Production app: `web`/`ws`/`worker`/`runner` process groups, one image, release command, health checks, `[[metrics]]`, VM sizing/counts (§3.2). |
| [`../fly/fly.kafka.toml`](../fly/fly.kafka.toml) | Internal-only single-broker KRaft Kafka app (`dataforge-kafka`), no public IP, 100 GB volume (§3.4). |
| [`../fly/fly.toml`](../fly/fly.toml) | Phase-1 throwaway (web-only `/healthz`); kept for history. `fly.prod.toml` supersedes it. |
| [`../scripts/prod-smoke.sh`](../scripts/prod-smoke.sh) | Post-deploy smoke: signup→verify→login→workspace→key→stream→start→events→stop, asserting each step, against any base URL (incl. the local compose for self-test). |

> **No secret value is committed anywhere.** Every credential is set with
> `fly secrets set` (rule S-2); this README documents secret **names** only.
> GitGuardian/gitleaks scan all commits.

---

## 1. One-time app + infra provisioning (per environment)

### 1.1 Create the apps

```sh
flyctl apps create dataforge            # main app (4 process groups)
flyctl apps create dataforge-kafka      # internal broker app (NO public IP)
```

The Kafka app must never get a public IP (D-4). Verify it has none:

```sh
flyctl ips list --app dataforge-kafka   # expected: empty
```

### 1.2 Managed Postgres attach (Fly Managed Postgres, §3.5)

GA plan: 4 vCPU / 16 GB / 500 GB, single primary, daily snapshots + WAL (7-day
PITR). Create and attach so the connection string lands as a secret:

```sh
flyctl mpg create --name dataforge-pg --region iad --plan "4vcpu-16gb" --volume-size 500
# Attach injects DATABASE_URL into the app's secrets (do NOT echo it):
flyctl mpg attach dataforge-pg --app dataforge
```

The runtime connects as the **NOBYPASSRLS** role (so RLS bites, SEC-TEN-2).
Migrations + `provision_db_roles` use the **owner** role via a *second* secret,
`MIGRATE_DATABASE_URL` (set manually to the owner DSN — see §2). Both DSNs
target the same database; they differ only in role.

### 1.3 Managed Redis attach (Upstash via Fly, §3.5)

GA plan: 3 GB provisioned, **`noeviction`** (mandatory — leases/checkpoints/
pools/revocation cache are correctness-bearing, never evictable). Single logical
DB with mandatory `df:*` key prefixes (§3.6).

```sh
flyctl redis create --name dataforge-redis --region iad --plan "3gb" --eviction noeviction
# Wire the URL into secrets (names-only here; the create command prints the URL):
flyctl secrets set --app dataforge REDIS_URL="<from the create output>"
```

### 1.4 Internal Kafka broker (`dataforge-kafka`, §3.4)

```sh
flyctl volumes create kafkadata --app dataforge-kafka --size 100 --region iad
# CLUSTER_ID is a fixed KRaft format id (NOT a credential), stable across
# restarts so re-up never reformats the volume. Generate once, store as a secret:
#   kafka-storage.sh random-uuid      # produces e.g. a 22-char base64url id
flyctl secrets set --app dataforge-kafka KAFKA_CLUSTER_ID="<generated-once>"
flyctl deploy --app dataforge-kafka --config infra/fly/fly.kafka.toml
```

Topics are created by the main app's release command
(`provision_kafka_topics`), never auto-created and never altered in place
(`KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`). Partition count on
`df.delivery.events` stays **12 at GA** (sized for aggregate TPS, NOT per shard —
scaling-strategy §2.3); growth is a v2 topic, never an in-place repartition.

---

## 2. Fly secrets (NAMES ONLY — set via `fly secrets set`, never committed)

Per `deployment-architecture.md` §5. `[env]` in `fly.toml` carries non-secret
config only; these are the secrets the app refuses to boot without (the prod
required-env manifest is enforced in `config/settings/prod.py`).

### 2.1 Main app `dataforge` (web/ws/worker/runner)

| Secret name | Consumed by | Notes |
|---|---|---|
| `DJANGO_SECRET_KEY` | all groups | Django signing; rotate yearly/on suspicion. |
| `JWT_SIGNING_KEY` | web, ws | **Distinct** from `DJANGO_SECRET_KEY` (security §3.1.2); 90-day dual-key rotation. |
| `DATABASE_URL` | all groups | **NOBYPASSRLS runtime role** DSN (RLS-constrained). Injected by `mpg attach`. |
| `MIGRATE_DATABASE_URL` | release command only | **Owner role** DSN for `migrate` + `provision_db_roles`. Set manually. |
| `REDIS_URL` | all groups | Upstash DSN; `noeviction`. |
| `EMAIL_URL` | web | Provider DSN (Postmark in staging/prod) — carries the email API token, so it IS a secret. Dev uses `smtp://mailpit:1025`. |
| `CONSOLE_BASE_URL` | web | Public console origin for verification/reset links; non-secret but env-required, set as a secret or `[env]`. |
| `TIGRIS_ACCESS_KEY` | worker | Object storage for ledger Parquet archive + pg_dump artifacts (archive/backup jobs). |
| `TIGRIS_SECRET_KEY` | worker | Pairs with `TIGRIS_ACCESS_KEY`; 180-day rotation. |
| `SENTRY_DSN` | all groups + frontend build | Not secret-critical; rotate on leak. |

`KAFKA_BOOTSTRAP_SERVERS` is **configuration, not a secret** (lives in
`fly.prod.toml [env]` as `dataforge-kafka.internal:9092`) until the managed
cluster adds `KAFKA_SASL_USERNAME`/`KAFKA_SASL_PASSWORD` at migration (§5, D-3).

Set example (values are placeholders you supply at deploy time — never commit):

```sh
flyctl secrets set --app dataforge \
  DJANGO_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))')" \
  JWT_SIGNING_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(50))')"
flyctl secrets set --app dataforge MIGRATE_DATABASE_URL="<owner-role-dsn>"
flyctl secrets set --app dataforge EMAIL_URL="<postmark-dsn>" CONSOLE_BASE_URL="https://app.dataforge.dev"
flyctl secrets set --app dataforge TIGRIS_ACCESS_KEY="<...>" TIGRIS_SECRET_KEY="<...>" SENTRY_DSN="<...>"
```

### 2.2 Kafka app `dataforge-kafka`

| Secret name | Notes |
|---|---|
| `KAFKA_CLUSTER_ID` | Fixed KRaft format id (NOT a credential); stable across restarts. |

### 2.3 CI/CD (GitHub Actions environment secrets)

| Secret name | Scope | Notes |
|---|---|---|
| `FLY_API_TOKEN` | per environment (staging/prod) | Scoped deploy token; 90-day rotation (S-1). |

---

## 3. Deploy commands

Build context is `backend/` (the Dockerfile `runtime`/`prod` target). The SPA
must be present at `backend/frontend-dist/` (a CI artifact, §8.1) before the
image build.

```sh
# Main app (4 process groups from one image):
flyctl deploy backend --app dataforge --config infra/fly/fly.prod.toml
# Kafka app (internal broker), if not already up:
flyctl deploy --app dataforge-kafka --config infra/fly/fly.kafka.toml
```

The release command runs once before machine replacement (§7.3):
`migrate --noinput → provision_db_roles → provision_kafka_topics →
sync_builtin_scenarios`. A failing release command aborts the deploy with the
old machines untouched.

### 3.1 kill_timeout per group (machine-config, set with `flyctl scale`)

`fly.toml [[vm]]` does not carry `kill_timeout`; set it (and counts) per group:

```sh
flyctl scale count web=2 ws=2 worker=1 runner=2 --app dataforge
# kill_timeout via machine config (web 15s / ws 30s / worker 60s / runner 30s, §3.2):
flyctl scale --app dataforge   # interactive; or set per-machine config in the platform
```

- `ws` 30 s: socket drain — clients reconnect + resume-from-cursor (ADR-0016/0013).
- `worker` 60 s: finish task or requeue; **beat is pinned here** via the Redis lock.
- `runner` 30 s: checkpoint + lease release on SIGTERM (D-5); ungraceful kills are
  covered by lease failover ≤ 30 s.

---

## 4. Environment promotion: dev → staging → prod (§6)

Config-only differences between environments (D-3); the *complete* diff is
`DF_ENV`, `DJANGO_SETTINGS_MODULE`, `DATABASE_URL`, `REDIS_URL`,
`KAFKA_BOOTSTRAP_SERVERS`, `ALLOWED_HOSTS`/CORS, email backend, Sentry env, log
level, machine counts/sizes, quota-headroom constants. Anything else differing
between environments is a defect.

| | dev | staging | prod |
|---|---|---|---|
| Substrate | Docker Compose (local) | Fly `dataforge-staging` + `dataforge-kafka-staging` | Fly `dataforge` + `dataforge-kafka` |
| Deploy trigger | manual / file-watch | **auto on merge to `main`** | **git tag `vX.Y.Z` + manual approval** (GitHub environment protection) |
| Image | local `dev` target, bind mounts | CI-built, **digest-pinned** | **same digest staging verified** — build once, promote twice |
| Key prefix | `df_dev_` | `df_stg_` | `df_live_` |
| Data | disposable | synthetic only; reset any time | real tenants; restore-tested backups |

Promotion flow (§6.2):

```
merge to main
  → CI builds + tests (gitleaks, ruff, mypy, pytest, OpenAPI/TS drift, e2e)
  → staging deploy (flyctl deploy --image <digest>, both apps)
  → automated staging smoke:  infra/scripts/prod-smoke.sh https://staging.dataforge.dev
  → human tags vX.Y.Z → approval gate
  → prod deploy of the IDENTICAL image digest (rolling, order worker→runner→ws→web, RB-1)
  → post-deploy verify: prod-smoke.sh + readyz all groups + 15 min quiet dashboards
```

There is no direct-to-prod lane; hotfixes follow the same path.

### 4.1 Staging config differences (`fly.staging.toml`, derived from `fly.prod.toml`)

Staging uses the same topology with smaller machines (`shared-cpu-1x`/1 GB;
worker `shared-cpu-2x`), `DF_ENV=staging`, `DJANGO_SETTINGS_MODULE=config.settings.staging`,
`ALLOWED_HOSTS=staging.dataforge.dev`, and its own `dataforge-kafka-staging`
internal app. **Staging caveat:** MAN-D604 dry-run throughput floors are
calibrated on `performance-2x` workers; staging's shared-CPU worker reports
indicative numbers only — manifest publication gating runs on prod-class
hardware (prod worker) or a pinned CI runner class.

---

## 5. Pre-decided 5th `sink` process-group split (CONDITIONAL — documentation only)

Per `deployment-architecture.md` §3.3, the decision to split the sink consumers
(buffer-writer + ws-pusher) out of the `runner` group into a dedicated `sink`
group is **already made**; only the timing is event-driven. Until the trigger
fires, the four ADR-0015 groups (`web`/`ws`/`worker`/`runner`) are the complete
production set, and `runner --role all` co-locates the sinks for machine economy.

**Trigger (fires when EITHER holds):**

1. **Sink CPU > 25 % of a runner machine** — measured from runner-machine CPU
   attributed to the sink consumer threads (the buffer-writer + ws-pusher
   `df_kafka_consumer_fetch_total` / commit work), OR
2. **Sustained aggregate TPS > 2,500** — trailing-window platform delivered TPS
   (`df_events_served_total` + WS fanout) over the alerting window.

**Execution (config-only, same image — D-2/D-3):** add a process group to
`fly.prod.toml` and move the sink role off the runner:

```toml
[processes]
  # runner becomes generation-only:
  runner = "python -m runner --role generation"
  # new dedicated sink group, same image:
  sink   = "python -m runner --role sinks"
```

Add a matching `[[vm]]` (`performance-2x`/4 GB), a `[[metrics]]` block
(`port 9091, path /metrics, processes = ["sink"]` — the sink process then hosts
its own 9091 exposer, one scrape target for the new group), and a runner-style
internal `/healthz` `[[services]]` check on `:8081`. No code change is required:
the same `python -m runner` entrypoint supports `--role generation|sinks|all`
(backend-architecture §8.1). This is **NOT** built in Phase 11 — it is the
documented next step when the trigger fires (recorded here so the runbook is
ready).

---

## 6. Backup / restore / retention (pointers)

Backup and retention jobs are Celery beat tasks on the `worker` group (§9.2,
P11-11); the restore drill is `infra/runbooks/restore-drill.sh` (P11-12, OPS-14).
Kafka volume snapshots (5-day) and Postgres PITR (7-day) are documented in the
runbooks (`infra/runbooks/`, P11-13). Loss semantics are the honest MVP posture
of §9.4 (bounded delivery-loss on Kafka volume loss; zero canonical loss — the
ledger write precedes publication, INV-GEN-5).
