# Runbook: DeliveryFreshnessBurn (PAGE)

SLO-2 delivery-success error budget is burning: delivered events are not visible to
the REST cursor within 30s of their canonical `emitted_at`. Multiwindow burn alert
(observability §8).

- **SLI:** `sli:slo2_delivery_success:ratio_rate{1h,6h}` (good = events with
  `df_buffer_commit_lag_seconds` ≤ 30s).
- **Source metrics:** `df_buffer_commit_lag_seconds` (histogram),
  `df_events_served_total{channel="rest"}`, `df_kafka_consumer_lag{group,topic,partition}`.
- **Target:** 99% over 30 days. (Durability is absolute, not budgeted: zero canonical
  loss in ledger retention, zero delivered loss in buffer retention.)

## Symptom

REST cursor consumers fall behind real time; the freshness lag (canonical
`emitted_at` → buffer visibility) exceeds 30s for a growing fraction of events. The
delivery pipeline (runner → Kafka → buffer-writer → `event_buffer`) is backing up.

## Diagnosis

1. Locate the lag in the chain:
   - **Kafka consumer lag** (buffer-writer falling behind):
     ```
     max by (group, topic) (df_kafka_consumer_lag{group="rest-buffer"})
     ```
     Growing lag → buffer-writer is the bottleneck (or the broker is degraded).
   - **Commit lag histogram** rising p95:
     ```
     histogram_quantile(0.95, sum by (le) (rate(df_buffer_commit_lag_seconds_bucket[5m])))
     ```
2. Check broker health: `df_kafka_broker_up` — if 0, this is
   [KafkaBrokerDown.md](KafkaBrokerDown.md) instead.
3. Check the buffer-writer (`runner` group hosts it): is it running, CPU-bound, or
   stuck on Postgres COPY? Inspect `df_buffer_writes_total{result}` for `error`/`retry`
   and `df_buffer_write_batch_size`.
4. Check `event_buffer` partition health: is the current-hour partition attached?
   A missing partition makes every write fail loudly (no DEFAULT partition, §8.1).
5. Check Postgres write health (the buffer COPY target): connections, lock contention,
   disk.

## Steps

- **Consumer lag growing, broker healthy** → the buffer-writer is under-provisioned or
  stuck. Restart it ([restart-buffer-writer.md](restart-buffer-writer.md)); it resumes
  from its committed offset. If sustained > 2,500 TPS, this is the pre-decided `sink`
  split trigger (deployment-architecture §3) — scale the sink group.
- **Broker degraded but up** → see [ConsumerLagGrowing.md](ConsumerLagGrowing.md); if
  down, [KafkaBrokerDown.md](KafkaBrokerDown.md).
- **Missing/expired buffer partition** → run the maintenance task to (re)create the
  current window: `celery -A config call streams.maintain_buffer_partitions` (or wait
  for the hourly beat). Verify the current-hour partition is attached.
- **Postgres write contention** → see Postgres in
  [restart-component.md](restart-component.md).

## Verification

- `df_kafka_consumer_lag{group="rest-buffer"}` drains to ~0.
- `df_buffer_commit_lag_seconds` p95 returns below 30s.
- `sli:slo2_delivery_success:ratio_rate1h` climbs back above the 99% target.
- A freshly emitted event becomes cursor-visible within 30s (spot probe).
- The 1h/5m burn windows drop below threshold and the alert resolves.
