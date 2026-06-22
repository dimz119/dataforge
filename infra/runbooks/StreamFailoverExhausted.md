# Runbook: StreamFailoverExhausted (PAGE)

A stream has exhausted shard failover and entered `failed`. This is **log-derived**
(observability §9): the runner emits a failover-exhausted event when a shard cannot be
re-acquired/restarted within its retry budget. The page rule references
`df_stream_failover_exhausted_total` (a counter the runner failover path / log-to-metric
exporter must expose — see the runner-workstream follow-up; until wired, this fires from
the log alert on `event=stream.failover.exhausted`).

- **Trigger:** a stream transitions to `failed` after repeated failover attempts.

## Symptom

A specific stream stops generating and is reported `failed` in the API/console. Unlike
a transient takeover ([RunnerLeaseTakeoverSpike.md](RunnerLeaseTakeoverSpike.md)), the
shard could not be recovered at all — its worker keeps dying on restart from
checkpoint, or no runner can acquire the lease.

## Diagnosis

1. Identify the stream + shard from the log line:
   ```
   event=stream.failover.exhausted   (carries stream_id, shard_id, reason)
   ```
2. Why does the shard fail on restart? Pull the worker's ERROR logs for that
   `stream_id`/`shard_id`:
   - **Poison checkpoint** — the restored checkpoint state crashes the engine
     deterministically (a bad arrival cursor / pool snapshot).
   - **Missing dependency** — the scenario/manifest/registry row the stream needs is
     gone or corrupt.
   - **Resource** — every runner that tries it OOMs/CPU-pegs.
3. Confirm it is isolated to this stream (other streams' shards are healthy) — if the
   whole fleet is churning, treat as
   [RunnerLeaseTakeoverSpike.md](RunnerLeaseTakeoverSpike.md) /
   [lease-failover-diagnosis.md](lease-failover-diagnosis.md) first.

## Steps

- **Poison checkpoint** → roll the shard back to an earlier checkpoint (the runner keeps
  the latest 3 per (stream, shard)); resume the stream. The arrival-cursor rebase makes
  continuation byte-identical from the chosen checkpoint.
- **Missing/corrupt dependency** → restore the dependency (registry/manifest); if it
  requires data restore, see [restore-drill.md](restore-drill.md) / RB-7.
- **Resource** → move the shard to a larger runner / reduce its load, then resume.
- After the root cause is fixed, transition the stream out of `failed` (operator resume)
  — data is intact (the ledger is ground truth; `failed` never deletes, INV-TEN-5).
- If unrecoverable, communicate to the workspace owner with the exact last-good
  `sequence_no` from the ledger.

## Verification

- The stream resumes `running`; its shards hold stable leases.
- Per-shard `sequence_no` is gapless and monotone across the failure boundary
  (INV-GEN-7) — no canonical gap, no duplication.
- `df_runner_streams_running` returns to the expected count.
- No further `stream.failover.exhausted` events for the stream; the alert resolves.
