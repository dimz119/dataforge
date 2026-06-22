# Runbook: Restart `kafka` (single KRaft broker)

DataForge runs a **single-broker KRaft Kafka** on a dedicated internal-only volume
(`kafkadata`). It is the known weakest link at MVP (deployment-architecture §4). A
clean restart with the volume intact is safe (the broker resumes from its log; sinks
resume from committed offsets). Volume loss is a different, bounded-loss scenario —
see [kafka-volume-loss.md](kafka-volume-loss.md) (RB-6).

## Symptom / when to use

- Broker unreachable (drives [KafkaBrokerDown.md](KafkaBrokerDown.md)).
- Planned broker maintenance (RB-5).
- Broker degraded (high errors, partial unavailability).

## Diagnosis

1. `df_kafka_broker_up` per group — 0 ⇒ down.
2. Broker VM/container: prod `fly status -a $FLY_APP` (Kafka VM) + `fly logs`; local
   `docker compose -p dataforge ps kafka` + `logs --tail=100 kafka`.
3. **Determine volume health** before restarting — process crash with intact volume
   (restart-safe) vs corrupt/lost volume (go to [kafka-volume-loss.md](kafka-volume-loss.md)).
4. Disk full? Free space / grow the volume first.

## Steps

### Planned maintenance (RB-5)
1. Announce (status page).
2. Verify sink lag ≈ 0: `df_kafka_consumer_lag` near 0.
3. Stop the broker. Runners' bounded producer queues fill → generation throttles
   gracefully via the backpressure chain (scaling-strategy §4.4); **streams never leave
   `running`**.
4. Restart:
   - Prod: `fly machines restart <kafka_machine> -a $FLY_APP`.
   - Local: `docker compose -p dataforge restart kafka`.
5. Confirm lag drains and publication resumes.

### Unplanned crash (volume intact)
- Restart as above; the broker recovers its log, sinks resume from committed offsets,
  runner producer queues drain.

### Volume lost/corrupt
- Do NOT keep restarting — follow [kafka-volume-loss.md](kafka-volume-loss.md) (RB-6):
  recreate from the latest snapshot (`infra/scripts/kafka-volume-snapshot.sh`) when
  usable, else fresh KRaft format + `provision_kafka_topics`, then reconcile the buffer
  tail `sequence_no` and post a delivery-gap notice.

## Verification

- `df_kafka_broker_up == 1` from all groups.
- `df_kafka_consumer_lag` drains; `df_kafka_publish_total{result="ok"}` resumes.
- `df_buffer_commit_lag_seconds` p95 back under 30s (SLO-2).
- Streams remained `running` throughout; a spot probe confirms fresh delivery.
