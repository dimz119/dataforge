# Runbook: ConsumerLagGrowing (PAGE)

A Kafka consumer group is falling behind: `df_kafka_consumer_lag` has a positive slope
across all partitions of a group for 10 minutes (observability §9). The consumer
(buffer-writer or another sink) is not keeping up with publication.

- **Source metric:** `df_kafka_consumer_lag{group,topic,partition}` (gauge),
  `df_kafka_consumer_fetch_total{group}`.
- **Alert expression:** `deriv(df_kafka_consumer_lag[10m]) > 0` sustained per group
  (PromQL has no monotone primitive; a positive 10m slope stands in).

## Symptom

Lag is climbing on a consumer group. If left unchecked it becomes
[DeliveryFreshnessBurn.md](DeliveryFreshnessBurn.md) (SLO-2 breach). The `df-kafka-sinks`
dashboard shows the lag series trending up.

## Diagnosis

1. Identify the group + topic:
   ```
   topk(5, max by (group, topic) (df_kafka_consumer_lag))
   ```
   The primary consumer is the REST buffer-writer group (`rest-buffer`).
2. Is the consumer alive and fetching? `df_kafka_consumer_fetch_total{group}` should be
   increasing; flat → the consumer is stuck/crashed.
3. Is the broker healthy? `df_kafka_broker_up` — if flapping, see
   [KafkaBrokerDown.md](KafkaBrokerDown.md).
4. Is the consumer CPU/IO-bound on its sink write? For the buffer-writer, check
   `df_buffer_write_batch_size`, `df_buffer_writes_total{result}`, and Postgres write
   health (the COPY target).
5. Is publication simply higher than steady-state capacity? Compare publish rate
   (`rate(df_kafka_publish_total{result="ok"}[5m])`) to fetch/commit rate. Sustained
   > 2,500 TPS is the `sink` split trigger (deployment-architecture §3).

## Steps

- **Consumer stuck/crashed** → restart the buffer-writer
  ([restart-buffer-writer.md](restart-buffer-writer.md)); it resumes from its committed
  offset.
- **Consumer healthy but under-provisioned** → scale the sink group (more buffer-writer
  capacity). If the trigger (sink CPU > 25% of a runner machine, or sustained
  > 2,500 TPS) is met, execute the pre-decided `sink` fifth-process-group split.
- **Postgres write contention** at the buffer COPY → resolve Postgres
  ([restart-component.md](restart-component.md)).
- **Broker degraded** → [KafkaBrokerDown.md](KafkaBrokerDown.md).

## Verification

- `deriv(df_kafka_consumer_lag[10m])` returns to ≤ 0 for the group; absolute lag drains.
- `df_kafka_consumer_fetch_total{group}` continues increasing.
- SLO-2 freshness stays inside budget (no escalation to DeliveryFreshnessBurn).
- The alert resolves once the slope is non-positive for the hold window.
