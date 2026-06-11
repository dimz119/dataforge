# ADR-0015 — Fly.io: one app with process groups; single-broker Kafka on a Fly volume with a pre-committed managed-Kafka migration trigger

**Deliverable:** D17

DataForge deploys to Fly.io as one application with four process groups — `web`, `ws`, `worker`, `runner` — sharing a single image, on managed Postgres and managed Redis; internal Kafka runs as a single KRaft broker on a dedicated, internal-network-only Fly machine with a volume, with a **pre-committed trigger** that converts it to managed Kafka when any of three conditions fires. This warranted an ADR because the deployment topology is where the "modest MVP, horizontally scalable architecture" tension is resolved in dollars and operational risk: get it wrong toward heaviness and the MVP burns money on idle managed services; get it wrong toward lightness with no exit plan and the first scale event becomes an unplanned re-architecture.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** production topology from the Phase 1 hello-world deploy through GA (Phase 11); the Kafka hosting posture and its exit condition; which infrastructure is managed vs self-run

## Context

The forces:

- **Fly.io is the mandated initial deployment target**, with Docker/Compose for dev. Dev–prod parity matters disproportionately here: the data plane (runners, leases, Kafka consumers) is the hardest part to debug, so the production shape should be the compose shape with different scaling numbers, not a different shape.
- **Process heterogeneity:** the platform is four distinct runtime roles — stateless WSGI API, stateful-connection ASGI WebSocket tier (ADR-0013), Celery control-plane worker (ADR-0006), and long-lived leased runners (data plane). They scale on different axes and must restart/deploy independently, but share one codebase, one settings module, one image.
- **Kafka hosting was a genuine panel disagreement.** P2: never run Kafka on Fly machines — managed from day one (ops burden, durability fear). P1/P3: a single internal-only broker on a Fly volume is acceptable MVP risk with an upgrade path. The requirements force Kafka into the stack from Phase 1 (ADR-0005), so "no Kafka yet" was never an option — only *where it runs*.
- **Durability asymmetry makes the cheap broker tolerable:** business truth lives in the Postgres ledger and the replayable buffer (ADR-0009, ADR-0013). Broker loss is a delivery-*availability* incident, never canonical data loss ([../02-architecture/system-architecture.md](../02-architecture/system-architecture.md) §9). A single broker would be indefensible if Kafka were the system of record; it is not.
- **The classic failure mode of "temporary" infrastructure** is that the upgrade decision gets re-litigated under incident pressure. The remedy is to make the decision now and leave only the timing event-driven.

## Decision

1. **One Fly app `dataforge`, four process groups, one image.** `web` (gunicorn WSGI — REST API + SPA static assets), `ws` (uvicorn ASGI Channels — WebSocket tails only, per ADR-0013), `worker` (Celery worker + beat — control plane only, per ADR-0006), `runner` (data-plane supervisor: shard runners + MVP sink consumers). Roles are command-selected from the same image in `fly.toml`; machine sizes, counts, health checks, and `kill_timeout`s are pinned in [../02-architecture/deployment-architecture.md](../02-architecture/deployment-architecture.md) §3.2. One `release_command` runs migrations and idempotent Kafka topic provisioning.
2. **Managed state stores.** Fly Managed Postgres (all durable state: control tables, ledger, buffer, injections, audit) and Fly-managed Upstash Redis with **`noeviction`** (leases, pools, revocation cache, channel layer, broker, counters — correctness-bearing state is never evictable). DataForge self-operates no database.
3. **MVP Kafka: single KRaft broker, internal-only, on a Fly volume.** A separate Fly app `dataforge-kafka` — one `performance-2x` machine, one 100 GB volume, **no public IP**, reachable only as `dataforge-kafka.internal:9092` on the org-private 6PN network (consumption-model boundary CB-1: no tenant credential for it exists in any phase). Single-node combined broker/controller, replication factor 1, short delivery-topic retention (6 h) because user-facing replay is served by the Postgres buffer, not Kafka (ADR-0013).
4. **Pre-committed managed-Kafka migration trigger** (Confluent/Redpanda/Upstash; any one clause sufficient):
   - the external Kafka delivery channel ships (Phase 12 entry task — hosted per-workspace topics need real SASL/ACL multi-tenancy and replication), **or**
   - sustained aggregate platform TPS exceeds ~5,000 (measured: trailing 7-day p95 of delivered TPS), **or**
   - the availability SLO is breached by broker incidents (measured: broker-attributed downtime consuming > 50% of the monthly data-plane error budget, [../02-architecture/observability.md](../02-architecture/observability.md) §7–8).
   The migration *decision* is made here; only its timing is event-driven. Operationalization and the migration playbook (MirrorMaker 2 bridge, sinks flip first, contract-suite verification, 48 h decommission window) live in deployment-architecture §4.
5. **The swap is infra-only by construction:** every producer and consumer reads `KAFKA_BOOTSTRAP_SERVERS` and consumer-group config from the environment; no code path knows where the broker lives. Single-region (`iad`) at MVP, stated honestly: the 99.9% availability target is a post-GA ladder (observability §7.4), not an MVP claim.

The topology at a glance:

| Component | Where | Managed? | Scaling lever |
|---|---|---|---|
| `web` (WSGI API + SPA assets) | Fly app `dataforge`, process group | self (stateless) | machine count |
| `ws` (Channels ASGI tails) | 〃 | self (stateless) | machine count |
| `worker` (Celery worker + beat) | 〃 | self | machine count (beat stays single) |
| `runner` (shard runners + MVP sinks) | 〃 | self | machine count + shard leases |
| Kafka (internal backbone) | Fly app `dataforge-kafka`, 1 VM + 100 GB volume, no public IP | self **until trigger** → managed | the migration trigger |
| Postgres | Fly Managed Postgres | managed | plan size; HA replica post-GA |
| Redis | Upstash via Fly, `noeviction` | managed | plan size |

## Alternatives considered

- **Managed Kafka from day one** — panel position P2 ("never on Fly machines"). Rejected per the resolved disagreement: at MVP scale (hundreds of TPS, zero external-topic consumers) a managed cluster is recurring cost for capabilities nothing uses yet, while the single internal broker's real risk — delivery downtime, never data loss — is bounded by the durability asymmetry above. P2's legitimate concern is answered by *pre-committing* the migration with measurable triggers rather than deferring the backbone (ADR-0005) or buying the cluster early.
- **Self-managed multi-broker Kafka cluster on Fly volumes** (3 × KRaft, RF 3). Rejected: it combines managed Kafka's cost with self-managed Kafka's ops burden — partition reassignment, rolling broker upgrades, and inter-broker network tuning on general-purpose VMs — to mitigate a failure mode (broker loss) that does not destroy data here. If DataForge needs broker HA, the trigger fires and the answer is managed, not more self-run brokers.
- **One Fly app per service** (separate apps for api/ws/worker/runner). Rejected: N apps multiply secrets distribution, release coordination, and image builds; cross-app version skew becomes possible mid-deploy. Process groups give independent scaling and rolling deploys per role while keeping one image, one secret set, one release command. (Kafka *is* a separate app — the one component with a volume, fixed identity, and no public surface.)
- **Kubernetes (EKS/GKE) instead of Fly.io.** Rejected: Fly.io is the mandated initial target, and the platform's needs (process groups, private network, volumes, managed Postgres/Redis) map directly onto Fly primitives; a cluster would add an entire operational discipline before the first user event. Nothing in the topology is Fly-proprietary — process groups translate to Deployments if a future migration is ever warranted.
- **Broker as a fifth process group inside the `dataforge` app.** Rejected: process groups share the app's deploy lifecycle — a routine API deploy must never restart the broker; the broker needs a volume and stable identity, and isolating it in its own no-public-IP app makes the "no user path to Kafka" boundary (CB-1) a network fact rather than a configuration discipline.

## Consequences

### Positive

- Dev–prod parity: compose services map 1:1 onto process groups running the same image and entrypoints; the Phase 1 throwaway deploy already exercises the final topology, and later phases only change counts and sizes.
- The migration trigger converts the riskiest infrastructure judgment into a pre-made decision with named metrics — incident pressure can accelerate it but never re-open it.
- Scaling any tier (more `web`, more `ws`, more `runner` machines) and the pre-decided sink-group split are `fly.toml` changes only ([../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md) owns the arithmetic).

### Negative

- The single broker and single Postgres primary are honest platform-wide failure domains for delivery and everything, respectively, until the post-GA availability ladder (managed Kafka → HA Postgres → multi-region) is climbed; the SLO posture says so explicitly (observability §7).
- Single-region MVP: a Fly `iad` outage is a full outage. Accepted and stated, not hidden.
- Running even one broker means owning KRaft upgrades, volume monitoring, and retention tuning until the trigger fires — bounded by internal-only exposure and the 6 h retention posture.

### Follow-ups

- [../02-architecture/deployment-architecture.md](../02-architecture/deployment-architecture.md): `fly.toml` process table, machine sizing, Kafka VM configuration, secrets, env promotion, and the migration playbook (§4).
- [../02-architecture/observability.md](../02-architecture/observability.md): SLO definitions and the error-budget accounting that arms trigger clause 3.
- Phase 11: load test demonstrating ≥ 5k aggregate TPS on this topology; Phase 12: executing the migration as an entry task of the external Kafka channel.
