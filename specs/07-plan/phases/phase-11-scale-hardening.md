# Phase 11 — Scale, Observability, Production Hardening (MVP GA)

**Deliverable:** D18 (phase doc)

This phase moves DataForge from "works on compose" to production posture and tags **MVP GA**: runner sharding with measured throughput limits, full observability (structured logs, metrics, SLOs, alerting, runbooks), quota enforcement with graceful degradation, and the production Fly.io topology of ADR-0015 with rehearsed backup/restore. The headline proof is a sustained ≥ 5,000 aggregate TPS load test with zero integrity or isolation violations, published together with the capacity-arithmetic staircase to 100k TPS from [../../02-architecture/scaling-strategy.md](../../02-architecture/scaling-strategy.md). Operational contracts implemented here were fixed in [../../02-architecture/deployment-architecture.md](../../02-architecture/deployment-architecture.md) and [../../02-architecture/observability.md](../../02-architecture/observability.md); quota tier values in [../../01-product/prd.md](../../01-product/prd.md) §7.

## Goal

> Production posture: sharded throughput with measured limits, full observability, quotas, deployed GA on Fly.io.

## Dependencies

- **Phases 5/6** — single-shard streaming runtime, leases, checkpoints (sharding generalizes them: shard model and `shard_id` envelope field have existed since Phase 0/5 precisely so this phase changes no contracts).
- **Phase 9** — chaos (the late-arrival buffer and injection recording must hold under sharding and load).
- **Phase 7** — console (quota meters, `paused_quota`/`paused_idle` recovery flows, activity export).
- **Phase 1** — Fly.io app shell and process groups (throwaway deploy exercised since then; this phase productionizes it).
- Specs: scaling-strategy.md (staircase + backpressure policy), observability.md (log schema, metrics catalog, SLO-1/2/3, alert catalog), deployment-architecture.md (prod topology, secrets, backup/retention jobs, runbook outline), [../../06-quality/security-architecture.md](../../06-quality/security-architecture.md) (GA checklist).

## Scope

1. **Runner sharding**: N shards per stream (shard count pinned at start), per-shard Redis leases + fencing, actors partitioned by hash of their PK-1 key, internal Kafka partitions sized per shard, per-shard gapless `sequence_no` (INV-GEN-7 unchanged); documented ordering semantics published in API docs (ordering is per `partition_key`; cross-shard interleaving is unordered).
2. **Backpressure policy** per scaling-strategy.md: WS drop-oldest with accurate drop-notice counts (INV-DEL-5), buffer-writer consumer-lag handling with lag metrics + alerts, batch REST reads (`limit` up to 1,000 events/request) as the bulk-consumption path.
3. **Observability**: structured JSON logs with `workspace_id`/`stream_id`/`request_id` context on every line (frozen field schema, observability.md §2.2); Prometheus-style metrics per the §4 catalog; SLO-1 (control-plane availability), SLO-2 (delivery success), SLO-3 (WS connect) measured with 30-day error budgets; multiwindow burn alerts; Grafana dashboards per process group.
4. **Quotas** (PRD §7 values): events/day metering, per-stream and aggregate TPS caps enforced at command time (INV-TEN-5), concurrent-stream limits, idle auto-pause (`paused_idle` with one-click resume + audit entry), per-key rate limits with `Retry-After`.
5. **Production Fly topology**: process groups `web`/`ws`/`worker`/`runner` from one image, managed Postgres + Redis, single-broker KRaft Kafka on an internal-only volume-backed VM; the pre-decided `sink` fifth process group split executes only if its trigger fires (sink CPU > 25 % of a runner machine or sustained > 2,500 TPS); secrets via Fly secrets, env promotion dev → staging → prod.
6. **Data lifecycle jobs**: buffer partition drops (24/48 h by plan), `generation.archive_ledger_partitions` (daily 02:00 — Parquet export to object storage, verify counts, drop), Kafka volume snapshots (5-day retention), Postgres backups; **restore rehearsal** of ledger/buffer backups into a clean environment.
7. **Runbooks** in `infra/runbooks/`: per-component restart, lease-failover diagnosis, Kafka volume loss (bounded delivery-loss posture), quota incident, deploy/rollback.
8. **GA security checklist** from security-architecture.md executed and signed off; **MVP GA tag** cut after all exit criteria pass.

## Non-goals

- **No managed-Kafka migration** — the pre-committed trigger (deployment-architecture §4) has not fired at ≤ 5k sustained TPS; it executes as a Phase 12 entry task.
- **No external delivery channels** — Phase 12.
- **No multi-region, Postgres HA, or 99.9 % availability claim** — GA states MVP SLO targets honestly (SLO-1 99.5 %, SLO-2 99 %, SLO-3 99.5 %); the post-GA availability ladder is observability.md §7.4.
- **No billing/self-serve plan changes** — a single quota row per workspace at the plan-tier values; plan assignment is operator-side.
- **No 100k TPS demonstration** — the GA gate is ≥ 5k measured; the staircase to 100k is published arithmetic with named bottlenecks per rung, not a load-test claim.
- **No OpenTelemetry exporter** — correlation fields are in every log line now; OTLP export is post-GA (observability.md §3.2).

## Tasks

- [ ] **P11-01** — Shard model: shard-count-at-start API field (quota-capped), per-shard lease acquisition + fencing tokens, actor-to-shard partitioning; OPS-1/2 kill-tests extended to multi-shard.
- [ ] **P11-02** — Kafka partition provisioning per shard; per-shard checkpoint isolation; GOLD-D continuation test at N = 4 shards.
- [ ] **P11-03** — Backpressure: WS drop-oldest with notice counts, consumer-lag metrics + `DeliveryFreshnessBurn` alert, batch REST endpoint parameter.
- [ ] **P11-04** — Structured logging: shared logging module emitting the frozen field schema across web/ws/worker/runner; zero-ERROR soak assertion wired.
- [ ] **P11-05** — Metrics: instrument the observability.md §4 catalog (API latency/error by route, Celery queue depth, runner tick/emit rates, Kafka lag, buffer write throughput, WS connections/drops, chaos injection counters).
- [ ] **P11-06** — SLOs + alerting: SLO recording rules, error-budget dashboards, multiwindow burn alerts (`ApiAvailabilityBurn`, `DeliveryFreshnessBurn`, `WsConnectBurn`), paging policy.
- [ ] **P11-07** — Quota enforcement: events/day metering counters, command-time checks (start/TPS/backfill), system pause to `paused_quota` with resume-on-headroom guard (T7), idle auto-pause job.
- [ ] **P11-08** — Per-key rate limits (Redis token bucket) with RFC 9457 `rate-limited` + `Retry-After`; key-level limit metrics.
- [ ] **P11-09** — Console: `QuotaMeter` bars on dashboard, `paused_quota`/`paused_idle` recovery flows, activity export.
- [ ] **P11-10** — Production deploy: fly.toml process groups, managed Postgres/Redis attach, internal Kafka VM + volume, secrets, staging promotion pipeline, post-deploy smoke job.
- [ ] **P11-11** — Retention/backup jobs: buffer partition drop, ledger archive-to-Parquet, Kafka volume snapshots, Postgres backup schedule; job-failure alerts.
- [ ] **P11-12** — Restore rehearsal: scripted restore drill (OPS-14) into a scratch environment; row counts + partition ranges verified against the backup manifest; drill documented as a runbook.
- [ ] **P11-13** — Runbooks for every component restart + the five incident classes; reviewed by a second engineer executing them cold.
- [ ] **P11-14** — LOAD-5K harness: k6 scenarios (cursor pollers, 50 WS tails, control-plane churn), integrity reservoir sampler, TEN spot probes during load; publish the measured-ceiling report feeding scaling-strategy.md.
- [ ] **P11-15** — GA checklist execution + sign-off; tag `v1.0.0-ga`.

## Demo script

```bash
# 1. Production smoke — the full core flow against the prod URL (read-only + one disposable workspace):
./infra/scripts/prod-smoke.sh https://app.dataforge.dev     # signup→workspace→key→stream→events→stop, asserts each step
# 2. Load test (gate run): 10 workspaces × 5 streams × 100 TPS, 30 min
k6 run infra/loadtest/load-5k.js                            # thresholds: p95 < 500 ms, error rate < 0.1%, zero 5xx
#    During the window: TEN spot probes run; integrity sampler validates 1% of delivered events
# 3. Quota degradation: exhaust a Free workspace's 1M events/day
./infra/scripts/quota-burn.sh $WS                           # stream transitions to paused_quota
curl -s .../streams/$SID | jq .status                       # "paused_quota"; resume rejected until headroom (T7 guard)
#    Console shows the QuotaMeter at 100% and the recovery explanation
# 4. Failover under shards: SIGKILL one shard's runner mid-load
fly machines stop $RUNNER_MACHINE                           # lease expires ≤ 15 s; takeover < 30 s; no canonical gaps
# 5. Restore rehearsal: restore last night's ledger/buffer backup into scratch, verify counts
./infra/runbooks/restore-drill.sh --target scratch          # exits 0 with row-count + partition-range report
# 6. Dashboards: SLO burn rates green; zero ERROR log lines during the load window
```

## Exit criteria

| # | Criterion | Proof ([../../06-quality/testing-strategy.md](../../06-quality/testing-strategy.md)) | Lane |
|---|---|---|---|
| 1 | Sustained **≥ 5,000 aggregate TPS for 30 minutes** with zero integrity violations (PROP-RI reservoir sampling) and zero isolation breaches (TEN probes during load); events p95 < 500 ms, error rate < 0.1 %, zero 5xx | LOAD-5K | gate run + weekly |
| 2 | The measured-ceiling report is published and the 1 → 100k TPS staircase in scaling-strategy.md cites it, with per-rung bottleneck + remedy arithmetic | LOAD-5K output + doc review | gate run |
| 3 | Production URL serves the full core flow | post-deploy smoke (core-loop API walkthrough vs prod) | deploy pipeline |
| 4 | Quota exhaustion pauses streams gracefully: `paused_quota` in API + UI, data intact, resume guarded on headroom; idle auto-pause emits audit + one-click resume | OPS-9/10 + E2E `stream-control.spec.ts` | merge |
| 5 | Restore-from-backup rehearsed: drill restores ledger/buffer into a clean environment with verified counts | OPS-14 | gate run |
| 6 | Multi-shard correctness: kill-test failover < 30 s per shard with fencing; per-shard `sequence_no` gapless; cross-restart continuation byte-identical at N = 4 | OPS-1/2 (multi-shard) + GOLD-D | merge |
| 7 | Tenant metering isolation: one workspace's consumption never increments another's counters | TEN §7.5(P11), INV-OBS-3 | PR (permanent) |
| 8 | Observability live: frozen log schema on every process group, SLO dashboards + burn alerts active, zero ERROR lines over SOAK-200 | SOAK-200 log assertions + alert dry-run | nightly + gate run |
| 9 | Every component restart and the five incident classes have runbooks executed cold by a non-author | runbook drill checklist | gate run |
| 10 | GA security checklist signed off; **MVP GA tagged** | security-architecture.md checklist | gate run |
