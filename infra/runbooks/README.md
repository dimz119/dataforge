# DataForge runbooks (Phase 11, P11-13)

Operational runbooks for DataForge MVP GA. Each is concrete and executable
(symptom → diagnosis → steps → verification), matching the deployment-architecture §10
runbook outline. Local commands target the compose stack (`docker compose -p dataforge
…`); prod commands target Fly (`fly … -a $FLY_APP`). No credentials appear in any
runbook or script.

## Per-component restart

| Component | Runbook |
|---|---|
| Index + shared rules | [restart-component.md](restart-component.md) |
| `web` (REST API, /metrics) | [restart-web.md](restart-web.md) |
| `ws` (WebSocket fan-out) | [restart-ws.md](restart-ws.md) |
| `worker` (Celery control plane) | [restart-worker.md](restart-worker.md) |
| `beat` (scheduler, inside worker) | [restart-beat.md](restart-beat.md) |
| `runner` (data plane) | [restart-runner.md](restart-runner.md) |
| `buffer-writer` (Kafka→buffer sink) | [restart-buffer-writer.md](restart-buffer-writer.md) |
| `kafka` (single broker) | [restart-kafka.md](restart-kafka.md) |

Postgres + Redis restart guidance is in [restart-component.md](restart-component.md).

## Page-alert runbooks (one per `page` alert — frozen names, observability §9)

Every `severity: page` alert in `infra/observability/prometheus/slo-alerts.yml` has a
runbook (referenced by its `runbook:` annotation):

| Alert (page) | Runbook |
|---|---|
| `ApiAvailabilityBurn` | [ApiAvailabilityBurn.md](ApiAvailabilityBurn.md) |
| `DeliveryFreshnessBurn` | [DeliveryFreshnessBurn.md](DeliveryFreshnessBurn.md) |
| `WsConnectBurn` | [WsConnectBurn.md](WsConnectBurn.md) |
| `KafkaBrokerDown` | [KafkaBrokerDown.md](KafkaBrokerDown.md) |
| `ConsumerLagGrowing` | [ConsumerLagGrowing.md](ConsumerLagGrowing.md) |
| `RunnerLeaseTakeoverSpike` | [RunnerLeaseTakeoverSpike.md](RunnerLeaseTakeoverSpike.md) |
| `StreamFailoverExhausted` | [StreamFailoverExhausted.md](StreamFailoverExhausted.md) |
| `LateBufferOverdue` | [LateBufferOverdue.md](LateBufferOverdue.md) |
| `BeatDead` | [BeatDead.md](BeatDead.md) |

`ticket`-severity alerts (`ApiLatencyP99`, `CheckpointStale`, `BufferRetentionStalled`,
`CeleryQueueBacklog`, `AuthFailureSpike`, `QuotaPauseSpike`, `CursorExpiredSpike`) do not
require a dedicated page runbook; their triage is folded into the component/incident
runbooks above (e.g. `QuotaPauseSpike` → [quota-incident.md](quota-incident.md),
`CeleryQueueBacklog` → [restart-worker.md](restart-worker.md), `BufferRetentionStalled` →
[restart-worker.md](restart-worker.md) + the retention jobs).

## Incident classes (deployment-architecture §10 RB-x)

| Incident | Runbook | RB |
|---|---|---|
| Lease-failover diagnosis | [lease-failover-diagnosis.md](lease-failover-diagnosis.md) | RB-4 |
| Kafka volume loss / broker rebuild | [kafka-volume-loss.md](kafka-volume-loss.md) | RB-6 |
| Quota incident | [quota-incident.md](quota-incident.md) | — |
| Deploy / rollback | [deploy-rollback.md](deploy-rollback.md) | RB-1/2/3 |
| Restore rehearsal / Postgres restore | [restore-drill.md](restore-drill.md) + [restore-drill.sh](restore-drill.sh) | RB-7 |

## Security-incident runbooks (GA-SECURITY-CHECKLIST item 15; security-architecture §13.2)

| Incident | Runbook |
|---|---|
| Leaked workspace API key (revoke / confirm <1s / audit / rotate) | [security-leaked-key.md](security-leaked-key.md) |
| Platform secret rotation (SECRET_KEY / JWT_SIGNING_KEY / DB / Kafka via Fly secrets) | [security-key-rotation.md](security-key-rotation.md) |
| Abuse wave (signup/auth flood; captcha flip, RL tightening, edge block) | [security-abuse-wave.md](security-abuse-wave.md) |
| Suspected cross-tenant exposure (sev-1; 3-layer RLS verification, evidence capture) | [security-cross-tenant-suspected.md](security-cross-tenant-suspected.md) |

## Backup / retention scripts (P11-11/12)

| Script | Purpose | Runnable against |
|---|---|---|
| `infra/scripts/pg-backup.sh` | Nightly Postgres logical dump (control plane; excludes buffer/ledger data) | compose PG + prod |
| `infra/scripts/kafka-volume-snapshot.sh` | Daily Kafka volume snapshot (5-day retention) | compose volume + Fly |
| `infra/runbooks/restore-drill.sh` | Scripted restore rehearsal (counts + ranges vs manifest) | compose PG (scratch) |
| `generation.archive_ledger_partitions` (Celery beat, daily 02:00) | Ledger partition → Parquet, verify, drop | compose PG + prod |
| `streams.maintain_buffer_partitions` (Celery beat, hourly) | Buffer partition create-ahead + drop-past-retention | compose PG + prod |

All shell scripts pass `bash -n`; `restore-drill.sh` is exercised against the live
compose Postgres (exits 0 on a verified restore, 1 on any count/range mismatch).
