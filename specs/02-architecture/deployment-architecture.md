# DataForge — Deployment Architecture

**Deliverable:** D13

This document defines how DataForge runs: the single-command Docker Compose development stack (the same nine services, in the same shape, as production from Phase 1 onward), the Fly.io production topology per ADR-0015 (one app with `web`/`ws`/`worker`/`runner` process groups sharing one backend image, managed Postgres and Redis, a single-broker KRaft Kafka on a dedicated internal-only Fly VM with a pre-committed managed-Kafka migration trigger), secrets management, environment promotion dev → staging → prod, the CI/CD pipeline, the image build strategy, backup/restore and partition-retention operations, and the deploy/rollback runbook outline. Capacity numbers per rung live in [scaling-strategy.md](scaling-strategy.md); SLO definitions live in [observability.md](observability.md); process responsibilities follow [../03-domain/domain-model.md](../03-domain/domain-model.md) (Celery **worker** = control plane only; **runner** = data plane, ADR-0006). ADRs are indexed in [../adr/README.md](../adr/README.md).

---

## 1. Topology principles

| # | Principle | Consequence |
|---|---|---|
| D-1 | **Final shape from Phase 1.** The dev stack contains every production service — including Kafka — before any feature uses it (ADR-0005). No later phase changes the topology shape; phases only replace stub commands with real ones. | `docker compose up` at Phase 1 starts all nine services healthy; the Phase 1 exit criterion (`/readyz` green over Postgres/Redis/Kafka) probes the final wiring. |
| D-2 | **One backend image, many commands.** All backend process groups (`web`, `ws`, `worker`, `runner`) run the identical image; groups differ only by start command (ADR-0015). The frontend SPA is built in CI and baked into the backend image (§8). | A deploy is one image digest; "works in `web`, broken in `runner`" version skew is impossible. |
| D-3 | **Config-only environment differences.** Dev, staging, and prod differ exclusively in environment variables and machine counts/sizes — never in code paths or service topology. Broker endpoints, database URLs, and Redis URLs are config, which is what makes the managed-Kafka swap infra-only (ADR-0015). | The config matrix (§6.3) is the complete diff between environments. |
| D-4 | **Internal Kafka is invisible infrastructure.** Users never reach internal Kafka on any environment: in dev it has no business listener on the host beyond a tooling port; in prod the broker has no public IP at all (consumption-model boundary, [system-architecture.md](system-architecture.md)). | Per-workspace *external* topics (Phase 12) are a separate, credentialed surface on the managed cluster — never the internal broker. |
| D-5 | **Data plane survives deploys.** Runner machines drain gracefully (checkpoint + lease release on SIGTERM); lease failover (≤ 30 s, domain model §4.3) covers ungraceful replacement. A routine deploy never loses canonical events — the ledger write precedes publication (INV-GEN-5). | Rolling deploys are safe at any time; the runbook (§10) only adds care for migrations and broker maintenance. |

---

## 2. Docker Compose development stack

One command — `docker compose up` from `infra/compose/` — brings up the full platform. The stack lands in Phase 1 with stub commands for `runner` and `buffer-writer` and is feature-complete by Phase 6 ([../07-plan/phases/phase-01-foundations.md](../07-plan/phases/phase-01-foundations.md)).

### 2.1 Service table

| Service | Image / build target | Command | Ports (host → container) | Volume | Depends on (healthy) | Healthcheck (cmd · interval/timeout/retries/start) |
|---|---|---|---|---|---|---|
| `postgres` | `postgres:16-alpine` | default | `5432 → 5432` | `pgdata:/var/lib/postgresql/data` | — | `pg_isready -U dataforge -d dataforge` · 5s/3s/10/5s |
| `redis` | `redis:7-alpine` | `redis-server --appendonly yes` | `6379 → 6379` | `redisdata:/data` | — | `redis-cli ping` · 5s/3s/10/3s |
| `kafka` | `apache/kafka:3.7.0` | KRaft combined broker+controller (§2.2) | `19092 → 19092` (host tooling listener only) | `kafkadata:/var/lib/kafka/data` | — | `kafka-broker-api-versions.sh --bootstrap-server localhost:9092` · 10s/5s/10/30s |
| `api` | `backend` image, target `dev` | `python manage.py runserver 0.0.0.0:8000` (entrypoint: `migrate` + `provision_kafka_topics`, dev only) | `8000 → 8000` | bind mount `../../backend:/app` | postgres, redis, kafka | `curl -fsS localhost:8000/healthz` · 10s/5s/5/15s |
| `ws` | `backend`, target `dev` | `uvicorn config.asgi:application --host 0.0.0.0 --port 8001 --reload` | `8001 → 8001` | bind mount | postgres, redis | `curl -fsS localhost:8001/healthz` · 10s/5s/5/15s |
| `worker` | `backend`, target `dev` | `celery -A config worker -B -Q control,lifecycle,validation,exports,maintenance -c 2` (queues per [backend-architecture.md](backend-architecture.md) §7.1; beat embedded in dev only) | — | bind mount | postgres, redis, kafka | `celery -A config inspect ping -d celery@$$HOSTNAME` · 30s/10s/3/20s |
| `runner` | `backend`, target `dev` | `python -m runner --role generation` (Phase 1: stub that heartbeats and serves health) | `8090 → 8081` (health, debug only) | bind mount | postgres, redis, kafka | `curl -fsS localhost:8081/healthz` · 10s/5s/5/15s |
| `buffer-writer` | `backend`, target `dev` | `python -m runner --role sinks` — hosts the `rest_buffer` buffer-writer and the ws-pusher Kafka→channel-layer sink consumers (Phase 1: stub) | `8091 → 8081` (health, debug only) | bind mount | postgres, redis, kafka | `curl -fsS localhost:8081/healthz` · 10s/5s/5/15s |
| `web` | `frontend` image, target `dev` | Vite dev server `--host 0.0.0.0 --port 5173`, proxying `/api → api:8000`, `/ws → ws:8001` | `5173 → 5173` | bind mount `../../frontend:/app` (anonymous volume over `node_modules`) | api | `curl -fsS localhost:5173/` · 10s/5s/5/20s |
| `mailpit` | `axllent/mailpit` | default (SMTP capture + web UI/REST API) | `8025 → 8025` (UI + `/api/v1/messages`); SMTP `1025` internal-only | — | — | `mailpit readyz` · 10s/5s/5/5s |

The runner-family containers run the single data-plane entrypoint `python -m runner [--role generation|sinks|all]` ([backend-architecture.md](backend-architecture.md) §8.1) and expose its internal health listener `:8081` under distinct host ports (`8090`/`8091`) so both are curl-able from the host. `mailpit` is **dev-only tooling** — it captures the verification/reset emails that `EMAIL_URL` (§2.3) sends over SMTP and has no production counterpart (staging/prod use Postmark, §5). The "nine services" production-parity statements (D-1, Phase 1 exit criteria) count the nine platform services above it; mailpit is a tenth container in the file, exempt from the parity claim.

Named volumes: `pgdata`, `redisdata`, `kafkadata`. `docker compose down -v` is the documented full-reset; volumes otherwise persist across restarts so dev streams resume from checkpoints exactly as in prod.

### 2.2 Kafka dev configuration (single-node KRaft)

```yaml
environment:
  KAFKA_NODE_ID: 1
  KAFKA_PROCESS_ROLES: broker,controller
  KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka:9093
  KAFKA_LISTENERS: BROKER://:9092,CONTROLLER://:9093,HOST://:19092
  KAFKA_ADVERTISED_LISTENERS: BROKER://kafka:9092,HOST://localhost:19092
  KAFKA_INTER_BROKER_LISTENER_NAME: BROKER
  KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: BROKER:PLAINTEXT,CONTROLLER:PLAINTEXT,HOST:PLAINTEXT
  KAFKA_AUTO_CREATE_TOPICS_ENABLE: "false"
  KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
  KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
  KAFKA_LOG_DIRS: /var/lib/kafka/data
  CLUSTER_ID: ${KAFKA_CLUSTER_ID}   # fixed constant in .env so re-up never reformats
```

- `BROKER://kafka:9092` is the only listener application services use; `HOST://localhost:19092` exists solely for dev tooling (`kcat`, `kafka-console-consumer`) and has no production counterpart (D-4).
- Auto-creation is off everywhere. Topics are provisioned by the idempotent management command `manage.py provision_kafka_topics`, which creates the internal topic set defined in [backend-architecture.md](backend-architecture.md) with the partition counts and retention settings of §3.4. Dev runs it from the `api` entrypoint; prod runs it in the release command (§7.3).

### 2.3 Dev environment file

`infra/compose/.env` (gitignored) is seeded from the committed `.env.example`:

| Variable | Dev value | Notes |
|---|---|---|
| `DF_ENV` | `dev` | Also the API-key env token: `df_dev_…` (ADR-0011) |
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` | Settings split per [backend-architecture.md](backend-architecture.md) §11 |
| `DJANGO_SECRET_KEY` | dev-only constant | Never reused outside dev |
| `DATABASE_URL` | `postgres://dataforge:dataforge@postgres:5432/dataforge` | |
| `REDIS_URL` | `redis://redis:6379/0` | Single logical DB, namespaced keys (§3.6) |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | The one variable the managed-Kafka swap changes |
| `KAFKA_CLUSTER_ID` | fixed UUID constant | Prevents KRaft reformat on volume reuse |
| `EMAIL_URL` | `smtp://mailpit:1025` | Captured by the dev-only `mailpit` service (§2.1, UI at `localhost:8025`); real provider in staging/prod (§5) |

---

## 3. Fly.io production topology (ADR-0015)

### 3.1 Overview

```mermaid
flowchart TB
    subgraph users["Internet"]
        BR[Browser - console SPA]
        CL[User pipelines - REST and WS, API key]
    end
    subgraph org["Fly.io organization - region iad, private 6PN network"]
        subgraph appmain["Fly app: dataforge (one image, 4 process groups)"]
            WEB["web ×2<br/>gunicorn WSGI<br/>shared-cpu-2x / 2 GB"]
            WSG["ws ×2<br/>uvicorn ASGI Channels<br/>shared-cpu-2x / 2 GB"]
            WRK["worker ×1<br/>celery worker + beat<br/>performance-2x / 4 GB"]
            RUN["runner ×2<br/>data-plane supervisor:<br/>shard runners + sink consumers<br/>performance-2x / 4 GB"]
        end
        subgraph appkafka["Fly app: dataforge-kafka (no public IP)"]
            KAF["kafka ×1<br/>single-broker KRaft<br/>performance-2x / 4 GB<br/>volume 100 GB"]
        end
        MPG[("Fly Managed Postgres<br/>4 vCPU / 16 GB / 500 GB")]
        UPS[("Upstash Redis (Fly-managed)<br/>3 GB, noeviction")]
        TIG[("Tigris object storage<br/>ledger Parquet archive,<br/>pg_dump artifacts")]
    end
    BR -->|HTTPS| WEB
    CL -->|HTTPS REST| WEB
    CL -->|WSS| WSG
    WEB --- MPG & UPS
    WSG --- UPS & MPG
    WRK --- MPG & UPS & KAF
    RUN --- MPG & UPS & KAF
    WRK -. archive jobs .-> TIG
```

All apps live in one Fly organization and share the private IPv6 network (6PN); `dataforge-kafka` is reachable only as `dataforge-kafka.internal:9092`. Primary region: `iad` (single-region MVP; the availability consequences are stated honestly in §11 and [scaling-strategy.md](scaling-strategy.md) §7).

### 3.2 Process groups (`fly.toml`)

```toml
app = "dataforge"
primary_region = "iad"

[processes]
  web    = "gunicorn config.wsgi:application -b [::]:8000 -w 4 --timeout 60"
  ws     = "uvicorn config.asgi:application --host '::' --port 8001 --workers 2"
  worker = "celery -A config worker -B -Q control,lifecycle,validation,exports,maintenance"   # beat embedded under the Redis singleton lock (backend-architecture §7.4)
  runner = "python -m runner --role all"   # generation shard runners + sink consumers (backend-architecture §8.1)

[deploy]
  release_command = "bash -c 'python manage.py migrate --noinput && python manage.py provision_kafka_topics'"
  strategy = "rolling"
```

| Group | Machine size | Count (GA) | kill_timeout | Fly health check | Role |
|---|---|---|---|---|---|
| `web` | `shared-cpu-2x` / 2048 MB | 2 | 15 s | HTTP `GET /readyz` :8000, 10s interval | Stateless REST API (control plane + REST event pulls) |
| `ws` | `shared-cpu-2x` / 2048 MB | 2 | 30 s (socket drain; clients reconnect + resume-from-cursor, ADR-0016/0013) | HTTP `GET /healthz` :8001 | Channels ASGI: WebSocket tails only |
| `worker` | `performance-2x` (2 dedicated vCPU) / 4096 MB | 1 | 60 s (finish task or requeue) | TCP :8092 (worker health sidecar port) | Celery control plane: lifecycle commands, schedules, snapshots, batch/backfill jobs, retention jobs, **layer-3 manifest dry runs** |
| `runner` | `performance-2x` / 4096 MB | 2 | 30 s (checkpoint + lease release on SIGTERM) | HTTP `GET /healthz` :8081 (internal listener, backend-architecture §8.1) | Data plane: shard runner processes (behavior + chaos), plus MVP sink consumers (§3.3) |

Notes binding other specs:

- The `worker` group uses the **same instance class as runners** (`performance-2x`) deliberately: layer-3 manifest dry runs measure `est_eps_per_shard` on this class, so the MAN-D604 ≥ 1,000 events/s floor transfers directly to runner capacity planning ([../04-engines/scenario-plugin-architecture.md](../04-engines/scenario-plugin-architecture.md) §8.4, [scaling-strategy.md](scaling-strategy.md) §2.1).
- `celery beat` runs exactly once, supervised inside the single `worker` machine. If the worker group later scales out, beat stays pinned to one machine via a Redis lock (standard single-beat pattern); the schedule itself is code, not state.
- Machine counts of 2 for `web`/`ws` exist for rolling-deploy continuity and machine-level redundancy, not throughput (capacity arithmetic: [scaling-strategy.md](scaling-strategy.md) §2.6–2.7).

### 3.3 What runs where (logical component → process group)

| Logical component | Dev service | Prod process group | Why |
|---|---|---|---|
| REST API (DRF, `/api/v1`) | `api` | `web` | Stateless WSGI |
| WebSocket endpoints (Channels) | `ws` | `ws` | Separate ASGI tier, independently scalable (ADR-0015 review note) |
| Celery worker + beat (control plane) | `worker` | `worker` | ADR-0006: lifecycle, schedules, batch jobs, retention, validation dry runs |
| Stream-runner shard processes (behavior + chaos stages) | `runner` | `runner` | ADR-0006/0009: leased, long-lived, reconciling |
| buffer-writer sink (`rest_buffer` channel) | `buffer-writer` | `runner` (`--role all`) | Kafka consumer group `df.sink.rest-buffer.v1` (backend-architecture §8.6); co-located on data-plane machines at MVP scale for machine economy |
| ws-pusher sink (Kafka → Redis channel layer) | `buffer-writer` | `runner` (`--role all`) | Consumer group `df.sink.websocket.v1` (backend-architecture §8.6); consumer-group semantics make instance count free of duplicate frames |
| Console SPA static assets | `web` (Vite dev server) | `web` (WhiteNoise from the backend image, §8) | One image (D-2) |

The dev `buffer-writer` container and the prod in-runner sink host execute the **same entrypoint** (`python -m runner`, role `sinks` vs `all` — backend-architecture §8.1); dev separates the sinks into their own container purely for log clarity. **Pre-decided split (Refined in Phase 11):** when sink CPU exceeds 25% of a runner machine or sustained aggregate TPS exceeds 2,500, sinks move to a dedicated fifth process group `sink = "python -m runner --role sinks"` — a config-only change to `fly.toml` using the same image. Until that trigger, the four ADR-0015 groups are the complete production set.

### 3.4 Kafka on Fly (dedicated VM, internal-only)

| Aspect | Value |
|---|---|
| App / machine | `dataforge-kafka`, 1 × `performance-2x` / 4096 MB, region `iad` |
| Storage | 1 × 100 GB Fly volume (local NVMe), `KAFKA_LOG_DIRS` on the volume |
| Network | **No public IP allocated.** Listener `BROKER://[fly-local-6pn]:9092`, advertised `dataforge-kafka.internal:9092`; controller listener `:9093` local only. PLAINTEXT on the private network for MVP (the 6PN is org-private and WireGuard-encrypted); SASL/ACL arrives with the managed cluster + external topics (Phase 12, [../06-quality/security-architecture.md](../06-quality/security-architecture.md)) |
| Mode | Single-node KRaft, combined broker+controller, replication factor 1 (stated honestly: a broker or volume loss interrupts in-flight delivery — §9.4, §11) |
| Internal topic retention | Delivery topics: `retention.ms = 6 h` **and** `retention.bytes = 5 GiB` per partition (12 partitions ⇒ ≤ 60 GiB, ≤ 60% of the volume). Sinks consume within seconds; 6 h covers sink-outage recovery. REST replay is served by the Postgres buffer (24–48 h), never by Kafka — so short broker retention costs users nothing (ADR-0013) |
| Partitions | 12 on `df.delivery.events` at GA; budgeting and growth in [scaling-strategy.md](scaling-strategy.md) §2.3 |
| Topic provisioning | `manage.py provision_kafka_topics` in the release command (idempotent; creates missing topics, never alters existing partitions) |
| Monitoring | JMX→Prometheus sidecar exposing broker metrics on the private network; alert rules in [observability.md](observability.md) |

### 3.5 Managed Postgres and managed Redis

| Service | Plan (GA) | Configuration | Used for |
|---|---|---|---|
| Fly Managed Postgres | 4 vCPU / 16 GB RAM / 500 GB volume, single primary at GA (HA replica is a post-GA availability step, §11) | Daily snapshots + WAL archiving, 7-day PITR window (§9.1); `max_connections` budgeted in [scaling-strategy.md](scaling-strategy.md) §2.7 | All control-plane tables, ground-truth ledger, event buffer, injection records, audit ([../03-domain/database-schema.md](../03-domain/database-schema.md)) |
| Upstash Redis (Fly-managed) | 3 GB provisioned target, `noeviction` | **`noeviction` is mandatory**: leases, checkpoint coordination, entity pools, and the revocation cache are correctness-bearing state, never evictable cache | Celery broker, API-key revocation cache, leases/heartbeats, entity-pool hot state, channel layer, stream-stats counters |

### 3.6 Redis key namespace (single logical DB)

Managed Redis providers commonly restrict `SELECT`; DataForge therefore uses one logical DB with mandatory prefixes (the prefix is part of the tenancy surface — pool, stats, and lease keys embed `workspace_id` per INV-TEN-1):

| Prefix | Owner | Examples |
|---|---|---|
| `df:broker:*` | Celery (kombu namespace option) | task queues |
| `df:revoke:*` | Security ([../06-quality/security-architecture.md](../06-quality/security-architecture.md)) | `df:revoke:{key_prefix}` |
| `df:lease:*` | Stream Control | `df:lease:{stream_id}:{shard_id}` |
| `df:pool:{workspace_id}:*` | Generation ([../04-engines/behavior-engine.md](../04-engines/behavior-engine.md)) | per-stream entity pools |
| `df:chan:*` | Channels layer | WS groups |
| `df:stats:{workspace_id}:*` | Observation | per-stream counters |

---

## 4. Managed-Kafka migration trigger (pre-committed)

The MVP broker posture is a deliberate, bounded risk. Per ADR-0015, the migration decision is **already made**; only the timing is event-driven. The trigger, verbatim:

> Pre-committed migration trigger to managed Kafka (Confluent/Redpanda/Upstash): **the external Kafka delivery channel ships, OR sustained aggregate TPS exceeds ~5k, OR the availability SLO is breached by broker incidents.** Broker endpoints are config, so the swap is infra-only.

Operationalization (measurable, evaluated monthly and at every Phase-12 planning checkpoint):

| Trigger clause | Measured as | Source |
|---|---|---|
| External Kafka channel ships | Phase 12 starts implementation — migration is a Phase 12 entry task, before any external topic is provisioned | [../07-plan/phases/phase-12-delivery-expansion.md](../07-plan/phases/phase-12-delivery-expansion.md) |
| Sustained aggregate TPS > ~5k | Trailing 7-day p95 of platform aggregate delivered TPS > 5,000 | `df_delivery_events_total` rate ([observability.md](observability.md)) |
| SLO breached by broker incidents | Broker-attributed downtime consumes > 50% of the monthly data-plane error budget in any calendar month | Incident review + SLO dashboard |

Migration playbook outline (full procedure written when the trigger fires; the seams are in place now because every producer/consumer reads `KAFKA_BOOTSTRAP_SERVERS` + per-component consumer-group config):

1. Provision the managed cluster (replication factor 3) in/peered to `iad`; provision topics via the same `provision_kafka_topics` command pointed at the new bootstrap.
2. Bridge with MirrorMaker 2 (internal topics are short-retention, so the mirror window is hours, not days).
3. Flip `KAFKA_BOOTSTRAP_SERVERS` for sinks first (consumers drain the old broker to its head, then resume on the new cluster from mirrored offsets), then runners (producers).
4. Run the cross-channel contract suite ([../06-quality/testing-strategy.md](../06-quality/testing-strategy.md)) against the new cluster; verify zero envelope diffs.
5. Decommission `dataforge-kafka` after one full buffer-retention window (48 h) of clean operation; keep its final volume snapshot 30 days.

---

## 5. Secrets management

| Rule | Statement |
|---|---|
| S-1 | Secrets live in exactly one place per environment: `infra/compose/.env` (gitignored) in dev; `fly secrets` per app in staging/prod; GitHub Actions environment secrets for CI/CD (only `FLY_API_TOKEN` per environment plus registry credentials). |
| S-2 | No secret ever appears in `fly.toml`, Dockerfiles, images, compose files, or repo history. `[env]` blocks in `fly.toml` carry non-secret config only. A gitleaks scan runs in CI on every PR. |
| S-3 | Secret changes are deploys: `fly secrets set` restarts machines group-by-group; rotation therefore follows the standard rolling-deploy safety of §10. |
| S-4 | Audit entries never contain secret material (INV-AUD-3); logs are scrubbed by the structured-logging layer ([observability.md](observability.md)). |

Secret inventory:

| Secret | Scope | Consumed by | Rotation |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | per env | web, ws, worker, runner | Yearly or on suspicion; rotating invalidates Django-signed artifacts (not JWTs — see next row) |
| `JWT_SIGNING_KEY` | per env, distinct from `DJANGO_SECRET_KEY` | web, ws | 90 days, dual-key overlap window so in-flight access tokens (short-lived, ADR-0011) survive rotation |
| `DATABASE_URL` | per env | all backend groups | On credential rotation of MPG |
| `REDIS_URL` | per env | all backend groups | With provider rotation |
| `EMAIL_API_KEY` (Postmark) | staging/prod | web (verification, reset mails) | 180 days |
| `TIGRIS_ACCESS_KEY` / `TIGRIS_SECRET_KEY` | prod | worker (archive/backup jobs) | 180 days |
| `SENTRY_DSN` | staging/prod | all backend groups + frontend build | Not secret-critical; rotate on leak |
| `FLY_API_TOKEN` (deploy) | CI per environment | GitHub Actions only | 90 days, scoped deploy tokens per app |

`KAFKA_BOOTSTRAP_SERVERS` is configuration, not a secret, until the managed cluster adds SASL credentials (`KAFKA_SASL_USERNAME`/`KAFKA_SASL_PASSWORD` join this table at migration; the config seam exists now — D-3).

---

## 6. Environment promotion: dev → staging → prod

### 6.1 Environments

| | dev | staging | prod |
|---|---|---|---|
| Substrate | Docker Compose (local) | Fly apps `dataforge-staging`, `dataforge-kafka-staging` | Fly apps `dataforge`, `dataforge-kafka` |
| Deploy trigger | manual / file-watch | **auto on merge to `main`** | **git tag `vX.Y.Z` + manual approval** (GitHub environment protection) |
| Image | built locally, `dev` targets, bind mounts | CI-built image, digest-pinned | **same digest staging verified** — build once, promote twice |
| Postgres | compose `postgres:16` | MPG 1 vCPU / 4 GB / 50 GB | MPG 4 vCPU / 16 GB / 500 GB |
| Redis | compose `redis:7` | Upstash 256 MB | Upstash 3 GB |
| Kafka | compose single KRaft | single KRaft, `shared-cpu-2x` / 2 GB / 20 GB volume | single KRaft per §3.4 (→ managed on trigger) |
| Machine counts | 1 each | 1 per group, `shared-cpu-1x` / 1 GB (worker `shared-cpu-2x`) | per §3.2 |
| Data | disposable | synthetic only; reset allowed any time | real tenants; restore-tested backups |
| `DF_ENV` / key prefix | `dev` / `df_dev_` | `staging` / `df_stg_` | `prod` / `df_live_` (token mapping owned by security-architecture §3.2.1) |

Staging caveat (stated so nobody trusts the wrong number): MAN-D604 dry-run throughput floors are calibrated on `performance-2x` workers; staging's shared-CPU worker reports indicative numbers only. Manifest publication gating runs on prod-class hardware (prod worker) or in CI on a pinned runner class.

### 6.2 Promotion flow

Merge to `main` → CI builds + tests → staging deploy → automated staging smoke (readyz across groups, scripted core-flow curl loop: signup → workspace → key → start stream → pull events → stop) → human tags `vX.Y.Z` → approval gate → prod deploy of the **identical image digest** → post-deploy verification (§10 RB-1). Hotfixes follow the same path; there is no direct-to-prod lane.

### 6.3 Config matrix (complete environment diff, per D-3)

`DF_ENV`, `DJANGO_SETTINGS_MODULE`, `DATABASE_URL`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`, `ALLOWED_HOSTS`/CORS origins, email backend, Sentry environment, log level, machine counts/sizes, quota headroom constants ([scaling-strategy.md](scaling-strategy.md) §5). Anything else differing between environments is a defect.

---

## 7. CI/CD pipeline

GitHub Actions, path-filtered per ADR-0001 (backend/, frontend/, infra/, specs/ jobs run only when touched; contract-artifact jobs run when either side or the contracts change).

### 7.1 Stages

```mermaid
flowchart LR
    A[PR opened] --> B["Stage 1: static<br/>ruff · mypy · eslint · tsc<br/>gitleaks"]
    B --> C["Stage 2: unit + integration<br/>pytest (compose services) · vitest"]
    C --> D["Stage 3: contract artifacts<br/>OpenAPI · manifest schema ·<br/>envelope schema · TS client drift"]
    D --> E["Stage 4: permanent gates<br/>cross-tenant attack suite ·<br/>CI scoping guard · golden-seed replay ·<br/>strip-boundary scan"]
    E --> F["Stage 5: build images<br/>backend (with SPA) + frontend dev<br/>push ghcr.io, tag sha"]
    F --> G["Stage 6: E2E<br/>Playwright vs compose stack"]
    G --> H{merge to main}
    H --> I["Deploy staging<br/>flyctl deploy --image digest"]
    I --> J["Staging smoke<br/>readyz + core-flow loop"]
    J --> K{"tag vX.Y.Z +<br/>manual approval"}
    K --> L["Deploy prod<br/>same digest, rolling"]
    L --> M["Post-deploy verify<br/>RB-1 checklist"]
```

| Stage | Jobs | Gate | First active phase |
|---|---|---|---|
| 1 Static | `ruff check`, `ruff format --check`, `mypy --strict`, `eslint`, `tsc --noEmit`, gitleaks, pre-commit parity | Any failure blocks | 1 |
| 2 Tests | `pytest` (Postgres/Redis/Kafka via compose services in CI), `vitest` | Coverage + suite green | 1 |
| 3 Contract artifacts | drf-spectacular OpenAPI export; manifest JSON Schema export; envelope JSON Schema export; regenerate TS client and fail on uncommitted diff (ADR-0014) | Contract drift blocks | 1 (OpenAPI), 3 (manifest/envelope) |
| 4 Permanent gates | Cross-tenant attack suite; unscoped-tenant-model CI guard (ADR-0002); golden-seed replay; `_df` strip-boundary scan (SB-3); zero-ecommerce-logic grep (P-1) | Never waived; failures block release | 2 / 2 / 4 / 5 / 3 |
| 5 Build | `docker buildx` backend runtime image (SPA baked in, §8) + dev images for E2E; push `ghcr.io/<org>/dataforge-backend:sha-<git>` | Build success + image scan (Trivy, high/critical) | 1 |
| 6 E2E | Playwright core-flow against the compose stack | Suite green | 7 |
| Deploy staging | `flyctl deploy --image <digest>` both apps; release command runs migrations + topic provisioning | Smoke green | 1 (hello-world), 11 (full) |
| Deploy prod | Same digest, approval-gated, rolling per group | RB-1 verification | 11 (GA) |

### 7.2 Artifacts

Every `main` build publishes: the backend image digest, the frontend `dist/` bundle (also baked into the image), `openapi.json`, `manifest-v0.schema.json`, `envelope-1.0.schema.json`, and the generated TS client package. These are the lockstep contracts of ADR-0001/0014.

### 7.3 Release command

Runs once per deploy, before machine replacement: `migrate --noinput` (under the N−1-compatible migration policy of §10 RB-3) then `provision_kafka_topics` (idempotent). A failing release command aborts the deploy with the old machines untouched.

---

## 8. Image build strategy

One multi-target Dockerfile per side (task contract); one **runtime** image per side per release.

### 8.1 `backend/Dockerfile`

| Target | Base | Contents | Used by |
|---|---|---|---|
| `base` | `python:3.12-slim` | OS deps (`libpq`, `librdkafka`), non-root user, `uv` | all |
| `deps` | `base` | `uv sync --frozen` (prod dependency set) | runtime |
| `dev` | `deps` | dev/test extras, no code copy (compose bind-mounts `/app`) | compose, CI tests |
| `runtime` | `deps` | `COPY backend/ /app`, `COPY frontend-dist/ /app/static/spa`, collectstatic (WhiteNoise) | **the** production image for all four process groups |

The SPA enters the backend image as a CI build artifact: the frontend job builds `dist/`, uploads it, and the backend image build copies it in. `web` serves it via WhiteNoise at `/` with hashed-asset immutable caching; `/api` and `/ws` are first-class routes. This is what makes "one app, one image" (ADR-0015) hold for the entire product surface.

### 8.2 `frontend/Dockerfile`

| Target | Base | Contents | Used by |
|---|---|---|---|
| `deps` | `node:22-slim` | `npm ci` | all |
| `dev` | `deps` | Vite dev server (HMR, proxy config) | compose `web` |
| `build` | `deps` | `tsc && vite build` → `/app/dist` | CI artifact stage (output feeds backend `runtime`) |

There is no production frontend container: prod static serving is the backend image's job (§8.1); the `dev` target exists for compose parity and fast HMR.

### 8.3 Tagging and provenance

Images tagged `sha-<gitsha>` (immutable) plus `staging`/`prod` floating aliases updated on deploy; deploys always pin digests, never floating tags. SBOM + Trivy scan attached per build. Base images bumped by a scheduled weekly CI job that opens a PR.

---

## 9. Backup, restore, and retention operations

DataForge's data has sharply different value classes, and the backup design says so explicitly:

| Data class | Tables | Loss tolerance | Mechanism |
|---|---|---|---|
| Control plane (users, workspaces, keys, scenarios, manifests, registry, streams, quotas, audit) | small, precious | none | MPG snapshots + PITR **and** nightly logical dump |
| Ground-truth ledger + injection records | large, gradable truth | none within retention | MPG snapshots/PITR; tiered to Parquet (§9.3) |
| Event buffer | large, derived, expiring (24–48 h) | re-derivable from ledger + chaos determinism (INV-G-4); excluded from logical dumps | partition TTL only |
| Kafka in-flight | seconds-to-hours of transit | bounded loss accepted at MVP (§9.4) | volume snapshots |
| Redis (leases, stats, pools) | ephemeral or snapshot-backed | rebuildable | entity-pool snapshots to Postgres ([../04-engines/behavior-engine.md](../04-engines/behavior-engine.md)); everything else regenerates |

### 9.1 Postgres backup

- **Physical:** MPG automated daily snapshots + continuous WAL archiving; PITR window 7 days. Snapshot retention: 7 daily + 4 weekly.
- **Logical:** nightly `pg_dump` of control-plane + registry + audit tables (explicitly excluding `event_buffer_*` and `ledger_*` partitions) to Tigris, 90-day retention — protects against logical corruption that PITR replays faithfully.
- **Restore (outline; full runbook RB-7):** restore snapshot/PITR to a new MPG cluster → run `manage.py check --database` + migration state verification → repoint `DATABASE_URL` via `fly secrets set` → rolling restart → verify readyz + the staging smoke loop against prod (read-only checks). Target RTO 60 min, RPO ≤ 5 min (WAL shipping interval).

### 9.2 Partition retention jobs (Celery beat, `worker` group)

Retention is enforced by **partition drop, never row deletes** (ADR-0013) — drops are O(1), generate no dead tuples, and keep autovacuum irrelevant on the hot path. Jobs land in Phase 11 ([../07-plan/phases/phase-11-scale-hardening.md](../07-plan/phases/phase-11-scale-hardening.md)); the contracts are fixed now:

| Job | Schedule (UTC) | Action |
|---|---|---|
| `delivery.drop_expired_buffer_partitions` | hourly at :05 | Drop `event_buffer` partitions older than the owning workspace's plan retention (24 h Free / 48 h Classroom+Pro, PRD §7); a cursor into a dropped partition yields `410 cursor-expired` (INV-DEL-4) |
| `generation.archive_ledger_partitions` | daily 02:00 | Export ledger partitions older than 48 h to Parquet on Tigris (zstd, ~5× compression), verify row counts, then drop the partition. The answer-key API serves the hot 48 h synchronously; older ground truth is served as an async export job (202 + poll) — contract fixed here, implementation refined in Phase 11 |
| `generation.enforce_ledger_retention` | daily 02:30 | Delete Parquet archives past total ledger retention (7 days default, domain model §2.6); injection records persist per stream lifetime + 30 days (they are tiny relative to events) |
| `chaos.gc_late_buffer` | hourly at :20 | Remove `emitted`/`discarded` late-arrival entries older than 24 h (pending entries are never GC'd — INV-CHA-5) |
| `generation.gc_checkpoints` | daily 03:00 | Keep the latest 3 checkpoints per (stream, shard); drop older |
| `tenancy.audit_retention` | monthly | Audit entries retained 13 months, then archived to Tigris (never silently dropped, INV-TEN-6) |
| `ops.pg_logical_dump` | daily 01:00 | §9.1 logical dump |

### 9.3 Ledger tiering (decided contract)

Hot window in Postgres: 48 h. Cold window: Parquet on Tigris through day 7. Consequences: chaos reads (real-time, post-ledger) and same-lab answer-key queries always hit the hot tier; week-old forensic grading is an async export. This keeps rung-4 Postgres storage bounded ([scaling-strategy.md](scaling-strategy.md) §3.4) without weakening the answer-key contract (ADR-0017).

### 9.4 Kafka and Redis loss semantics (honest MVP posture)

- **Kafka volume:** daily Fly volume snapshots (5-day retention). On broker/volume loss: recreate the machine (from snapshot when usable, else fresh KRaft format + `provision_kafka_topics`). Events published after the last sink consumption and before the crash are lost **from delivery only** — they remain in the ledger, and streams resume generating from checkpoints. Sinks resume from their committed offsets (or topic head on fresh format; the buffer's last `sequence_no` bounds the gap, which is surfaced in stream stats). This bounded-delivery-loss posture is exactly why "SLO breached by broker incidents" is a migration trigger clause (§4).
- **Redis loss:** leases expire (runners stop, then re-acquire); stats counters rebuild from buffer/ledger counts; entity pools restore from their latest Postgres snapshots + shard checkpoints — same path as runner failover, so it is exercised by the Phase 5 kill-test, not a bespoke recovery mode.

### 9.5 Restore rehearsal

Quarterly game-day: restore prod backup to a scratch MPG cluster, replay RB-7, measure RTO/RPO, file gaps. First rehearsal is a Phase 11 exit criterion ("restore-from-backup rehearsed").

---

## 10. Deploy/rollback runbook (outline)

Full step-by-step runbooks live in `infra/runbooks/` from Phase 11; the outline and the invariants each must honor are fixed here.

| ID | Runbook | Outline |
|---|---|---|
| RB-1 | **Standard deploy** | Tag + approve → release command (migrate + topics) → rolling per group, order `worker → runner → ws → web` (control plane first so new runner code never sees older desired-state schemas) → verify: readyz all groups, error-rate and consumer-lag dashboards quiet for 15 min, one scripted core-flow pass. |
| RB-2 | **Rollback (image)** | `fly releases` → redeploy previous digest (both apps if Kafka config changed). Safe at any time because migrations are N−1-compatible (RB-3). Verify as RB-1. |
| RB-3 | **Migration policy** | Expand/contract only: every migration must run against code version N−1 (additive columns nullable/defaulted; drops happen ≥ 1 release after code stops reading). **No down-migrations in prod** — a bad migration is rolled forward or restored via RB-7. CI runs the previous release's test suite against the new schema as the compatibility check. |
| RB-4 | **Runner drain/restart** | SIGTERM → runner finishes current tick, persists checkpoints, releases leases (≤ 30 s kill_timeout) → replacement machine acquires leases. Unplanned kill: lease TTL (15 s) + checkpoint restore ≤ 30 s total (Phase 5 kill-test). Pending late re-emissions survive either path (INV-CHA-5). |
| RB-5 | **Kafka planned maintenance** | Announce → verify sink lag ≈ 0 → stop broker; runners' bounded producer queues fill and generation throttles gracefully (backpressure chain, [scaling-strategy.md](scaling-strategy.md) §4.4) → restart → confirm lag drains; streams never leave `running`. |
| RB-6 | **Kafka volume loss / broker rebuild** | §9.4 procedure; explicitly reconcile buffer tail `sequence_no` per stream and post a status-page notice quantifying any delivery gap (the ledger quantifies it exactly). |
| RB-7 | **Postgres restore** | §9.1 procedure; decision tree for PITR-point selection; post-restore tenancy spot-audit (cross-tenant suite, read-only) before reopening writes. |
| RB-8 | **Secret rotation** | §5 inventory order; JWT dual-key overlap; verify auth + data-plane key checks post-rotation (revocation cache survives — it keys on hashes, not signing material). |
| RB-9 | **Region incident** | MVP posture: single region; declare on status page, wait or execute RB-7 restore into an alternate region with Kafka rebuild (RB-6) — documented as hours-scale RTO. Multi-region active-passive is the post-GA availability path (§11). |

---

## 11. Availability posture (honest statement)

The 99.9% availability target is a **post-GA roadmap item**, not an MVP property — single-region Fly with a single-broker Kafka and a single-primary Postgres cannot honestly promise it. The full arithmetic (component availabilities, composite math, error budgets, and the staged path to 99.9%) is owned by [scaling-strategy.md](scaling-strategy.md) §7; SLO definitions (control plane vs data plane vs WS) are owned by [observability.md](observability.md). The deployment-level redundancy facts:

| Layer | GA redundancy | Blast radius of single failure | Recovery |
|---|---|---|---|
| `web` / `ws` | 2 machines each | none (LB reroutes; WS clients reconnect + resume-from-cursor) | automatic |
| `worker` | 1 machine | delayed control-plane commands and schedules; data plane unaffected | Fly auto-restart, ≤ minutes |
| `runner` | 2 machines | leased shards fail over ≤ 30 s; no canonical loss | automatic (lease TTL) |
| Kafka | **1 broker** (known weakest link) | delivery pauses; bounded delivery gap on volume loss (§9.4) | RB-5/RB-6; managed migration per §4 |
| Postgres | managed, single primary, PITR | platform outage during restore | RB-7, RTO 60 min |
| Redis | managed (Upstash SLA) | leases/stats churn; pools restore from snapshots | automatic + §9.4 |
| Region | single (`iad`) | full outage | RB-9, hours-scale |

Path to 99.9% (sequenced, trigger- and post-GA-phase-driven): managed Kafka (§4) → Postgres HA replica → multi-machine `worker` → multi-region active-passive. Each step's availability gain is quantified in [scaling-strategy.md](scaling-strategy.md) §7.3.

---

## 12. Ownership boundaries

| Concern | Owner |
|---|---|
| Capacity arithmetic, TPS staircase, backpressure policy, quota numbers | [scaling-strategy.md](scaling-strategy.md) |
| SLO definitions, metrics catalog, alert rules, log schema | [observability.md](observability.md) |
| Internal Kafka topic naming, consumer-group layout, lease fencing | [backend-architecture.md](backend-architecture.md) |
| Table/partition DDL, RLS policies, index design | [../03-domain/database-schema.md](../03-domain/database-schema.md) |
| Key lifecycle, JWT mechanics, secrets-in-code rules, abuse controls | [../06-quality/security-architecture.md](../06-quality/security-architecture.md) |
| CI gate semantics per phase (which suite blocks which phase) | [../06-quality/testing-strategy.md](../06-quality/testing-strategy.md) |
| Phase sequencing and exit criteria referenced here | [../07-plan/incremental-roadmap.md](../07-plan/incremental-roadmap.md) |
