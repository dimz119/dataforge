# Runbook: KafkaBrokerDown (PAGE)

The single Kafka broker is unreachable. Alert fires when `df_kafka_broker_up == 0`
reported from at least two process groups for 1 minute (observability §9). Kafka is
the **known weakest link** at MVP (single broker, deployment-architecture §4).

- **Source metric:** `df_kafka_broker_up` (gauge, per process group).

## Symptom

`df_kafka_broker_up` is 0 across groups. Delivery pauses: the runner's bounded
producer queues fill, generation throttles via the backpressure chain, and the
buffer-writer stops committing. The ledger keeps recording (INV-GEN-5: ledger write
precedes publication), so **no canonical loss** — only delivery is impacted.

## Diagnosis

1. Confirm the broker is actually down (not a network blip from one group):
   `df_kafka_broker_up == 0` from ≥ 2 groups for ≥ 1m.
2. Check the broker VM/container:
   - Prod: `fly status -a $FLY_APP` (the Kafka VM), `fly logs` for the broker machine.
   - Local: `docker compose -p dataforge ps kafka` + `docker compose -p dataforge logs
     --tail=100 kafka`.
3. Determine the failure class:
   - **Broker process crashed, volume intact** → restart (data survives).
   - **Volume lost/corrupted** → this is RB-6 / [kafka-volume-loss.md](kafka-volume-loss.md)
     (bounded delivery-loss posture).
   - **Disk full** → free space / grow the volume, then restart.

## Steps

- **Process crash, volume intact** → restart the broker:
  - Prod: `fly machines restart <kafka_machine> -a $FLY_APP`.
  - Local: `docker compose -p dataforge restart kafka`.
  Sinks resume from their committed offsets; runners' producer queues drain.
- **Volume loss/corruption** → follow [kafka-volume-loss.md](kafka-volume-loss.md)
  (RB-6): recreate from the latest snapshot (`infra/scripts/kafka-volume-snapshot.sh`)
  when usable, else fresh KRaft format + `provision_kafka_topics`; reconcile the buffer
  tail `sequence_no` per stream and post a status-page notice quantifying the delivery
  gap (the ledger quantifies it exactly).
- **Repeated broker incidents consuming > 50% of the monthly data-plane budget** →
  this trips the managed-Kafka migration trigger (deployment-architecture §4); open the
  Phase 12 migration task.

## Verification

- `df_kafka_broker_up == 1` from all groups.
- `df_kafka_consumer_lag` drains back toward 0 (sinks caught up).
- `df_buffer_writes_total{result="ok"}` resumes; `df_buffer_commit_lag_seconds` p95
  back under 30s (SLO-2).
- Streams never left `running`; a spot probe confirms fresh events become
  cursor-visible.
- The alert resolves once the broker reports up for the hold window.
