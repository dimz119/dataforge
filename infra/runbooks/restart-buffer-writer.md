# Runbook: Restart `buffer-writer` (Kafka → event_buffer sink)

The buffer-writer consumes the internal Kafka topics and COPYs delivered events into
the hourly-partitioned `event_buffer` (the REST cursor's source). In prod it runs as a
thread inside the `runner` process (its metrics share the runner's `DF_METRICS_PORT`
exposer); in compose it is its own `buffer-writer` service. It is a Kafka consumer with
committed offsets — **a restart resumes exactly where it left off** (no delivery loss
within buffer retention).

## Symptom / when to use

- Kafka consumer lag growing on the `rest-buffer` group (drives
  [ConsumerLagGrowing.md](ConsumerLagGrowing.md) / [DeliveryFreshnessBurn.md](DeliveryFreshnessBurn.md)).
- `df_buffer_writes_total{result="error"|"retry"}` rising; COPY stuck.
- The current-hour `event_buffer` partition write failing (missing partition).

## Diagnosis

1. Is the consumer alive and fetching? `df_kafka_consumer_fetch_total{group="rest-buffer"}`
   increasing? `df_kafka_consumer_lag{group="rest-buffer"}` draining or growing?
2. `df_buffer_writes_total{result}`, `df_buffer_write_batch_size`,
   `df_buffer_commit_lag_seconds` p95.
3. Is the current-hour buffer partition attached? A missing partition makes every write
   fail loudly (no DEFAULT partition, §8.1) — run
   `streams.maintain_buffer_partitions` to recreate the window.
4. Postgres write health (the COPY target) — connections, locks, disk.
5. `service=buffer-writer level=error` recent lines.

## Steps

- **Prod (thread in runner):** restart the hosting runner machine
  ([restart-runner.md](restart-runner.md)); the buffer-writer thread restarts with it
  and resumes from its committed Kafka offset.
- **Local (standalone service):** `docker compose -p dataforge restart buffer-writer`.
- **Missing buffer partition** → `celery -A config call
  streams.maintain_buffer_partitions` (or wait for the hourly beat), then confirm the
  current-hour partition is attached; the writer's failing COPYs then succeed.
- **Under-provisioned at sustained > 2,500 TPS** → execute the pre-decided `sink`
  fifth-process-group split (deployment-architecture §3) to give the sink its own
  capacity.

## Verification

- `df_kafka_consumer_lag{group="rest-buffer"}` drains to ~0.
- `df_buffer_writes_total{result="ok"}` resumes; `error`/`retry` stop rising.
- `df_buffer_commit_lag_seconds` p95 back under 30s (SLO-2 freshness restored).
- A freshly emitted event becomes cursor-visible within 30s (spot probe).
