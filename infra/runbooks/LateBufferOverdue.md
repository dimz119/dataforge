# Runbook: LateBufferOverdue (PAGE)

The chaos late-arrival buffer is not draining: `df_chaos_late_buffer_overdue > 100`
for 5 minutes (observability §9). Entries whose `due_at` has passed are not being
re-emitted — INV-CHA-5 (pending late re-emissions must always eventually fire) is at
risk.

- **Source metrics:** `df_chaos_late_buffer_overdue` (gauge),
  `df_chaos_late_buffer_pending` (gauge), `df_chaos_reemissions_total{outcome}`.

## Symptom

A backlog of overdue late-arrival entries is accumulating. The scheduler that should
re-emit late events at their `due_at` has stalled. Chaos streams' late-arrival
correctness degrades; if entries are dropped instead of re-emitted, that violates
INV-CHA-5. The `df-chaos` dashboard shows `overdue` climbing while `reemissions_total`
flatlines.

## Diagnosis

1. Confirm the scheduler is alive and processing:
   ```
   rate(df_chaos_reemissions_total{outcome="emitted"}[5m])
   ```
   Flat near 0 while `overdue` grows → the re-emission scheduler is stuck.
2. Where does the re-emission run? It rides the runner data plane (the late buffer is
   keyed per stream). Check runner health for the affected streams
   ([restart-runner.md](restart-runner.md), [lease-failover-diagnosis.md](lease-failover-diagnosis.md)).
3. Is the broker/buffer downstream blocking re-emission writes? Correlate with
   `df_kafka_broker_up`, `df_kafka_consumer_lag`, `df_buffer_writes_total` — a stuck
   downstream backs up re-emission.
4. Are `outcome="error"` re-emissions rising? `sum by (outcome)
   (rate(df_chaos_reemissions_total[5m]))` — errors mean the re-emission path itself is
   failing (look at ERROR logs for `event=chaos.reemit.*`).

## Steps

- **Re-emission scheduler stuck on a runner** → restart the runner(s) hosting the
  affected chaos streams ([restart-runner.md](restart-runner.md)); on takeover the
  pending late entries survive (INV-CHA-5) and re-emission resumes from `due_at`.
- **Downstream (Kafka/buffer) blocking** → clear the downstream first
  ([KafkaBrokerDown.md](KafkaBrokerDown.md) / [DeliveryFreshnessBurn.md](DeliveryFreshnessBurn.md)),
  then re-emission drains.
- **Re-emission erroring** → fix the root cause from ERROR logs; pending entries are
  never GC'd (only `emitted`/`discarded` are), so nothing is lost while you fix forward.
- Do NOT delete pending late entries to clear the backlog — that breaks INV-CHA-5.

## Verification

- `df_chaos_late_buffer_overdue` drops back below 100 and trends to ~0.
- `df_chaos_reemissions_total{outcome="emitted"}` resumes increasing; `pending` drains.
- No `discarded` entries that should have been `emitted` (audit the affected streams).
- The alert resolves once overdue is below threshold for the hold window.
