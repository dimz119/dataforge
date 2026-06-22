# Incident runbook: Kafka volume loss / broker rebuild (RB-6)

The single Kafka broker's volume (`kafkadata`) is lost or corrupt. This is the
**bounded-delivery-loss** scenario fixed in deployment-architecture §9.4: events
published after the last sink consumption and before the crash are lost **from delivery
only** — they remain in the ground-truth ledger (INV-GEN-5), and streams resume
generating from checkpoints. The ledger quantifies the gap exactly.

## Symptom / when to use

- Broker won't start because its log/volume is corrupt, or the volume is gone.
- [KafkaBrokerDown.md](KafkaBrokerDown.md) diagnosis identified volume loss (not a clean
  process crash).

## Decision tree

1. **Volume usable from a snapshot?** Daily snapshots have 5-day retention
   (`infra/scripts/kafka-volume-snapshot.sh`).
   - **Yes** → recreate the broker machine restoring from the latest snapshot. Sinks
     resume from committed offsets; the gap is bounded by what was published after the
     snapshot.
   - **No (no usable snapshot)** → fresh KRaft format + re-provision topics
     (`provision_kafka_topics`). Sinks resume from the topic head; the gap is bounded by
     the buffer's last `sequence_no` per stream.

## Steps

1. **Stop the broken broker** so producers stop erroring against it (their bounded
   queues fill and generation throttles via backpressure — streams stay `running`).
2. **Recreate the broker:**
   - From snapshot (prod): create a new volume from the latest Fly snapshot, attach,
     start the Kafka machine.
     - Local rehearsal: restore the tar snapshot produced by
       `infra/scripts/kafka-volume-snapshot.sh` into the `kafkadata` volume, then
       `docker compose -p dataforge up -d kafka`.
   - Fresh format (no snapshot): start a clean broker, then run `provision_kafka_topics`
     to recreate the topic set (partition count stays **12** — sized for aggregate TPS,
     NOT per shard; growth is a v2 topic only, scaling §2.3).
3. **Reconcile the delivery gap per stream:** for each stream, compare the buffer tail
   `sequence_no` (last delivered) against the ledger's max `sequence_no` (ground truth).
   The difference is the exact set of events lost from delivery (none lost from the
   ledger). Surface this in stream stats.
4. **Post a status-page notice** quantifying the delivery gap (the ledger gives the
   exact count/range). Educate that re-derivation is possible from the ledger if needed
   (INV-G-4 determinism).
5. If broker incidents have consumed > 50% of the monthly data-plane error budget, this
   trips the **managed-Kafka migration trigger** (deployment-architecture §4) — open the
   Phase 12 migration task.

## Verification

- `df_kafka_broker_up == 1`; topics present (`provision_kafka_topics` idempotent check).
- `df_kafka_consumer_lag` drains; `df_buffer_writes_total{result="ok"}` resumes.
- Streams remained `running`; new events flow to delivery again (spot probe).
- The per-stream gap is reconciled and reported; the ledger shows **zero** canonical
  loss (the durability invariant held).
