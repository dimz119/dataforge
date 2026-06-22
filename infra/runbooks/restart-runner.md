# Runbook: Restart `runner` (data plane)

The `runner` process group IS the data plane (ADR-0006): it leases stream shards,
drives the engine, appends the ledger, publishes to Kafka, and (in-process) hosts the
buffer-writer and ws-pusher threads — all three metric families share the runner's
`DF_METRICS_PORT` exposer. Two machines in prod; leased shards **fail over ≤ 30s** with
fencing, so a restart loses no canonical events.

## Symptom / when to use

- A runner is crash-looping / OOMing (drives
  [RunnerLeaseTakeoverSpike.md](RunnerLeaseTakeoverSpike.md)).
- Tick overruns starving heartbeats (`df_runner_tick_overruns_total`).
- After a deploy (runner restarts after worker in the rolling order, RB-1).
- Draining a machine for maintenance.

## Diagnosis

1. `fly status -a $FLY_APP` / `docker compose -p dataforge ps runner` — which machine,
   restart count, OOM kills.
2. `df_runner_active_leases`, `df_runner_streams_running`,
   `df_runner_lease_takeovers_total{reason}`, `df_runner_tick_duration_seconds`,
   `df_runner_tick_overruns_total`.
3. Redis (lease store) health — flaky Redis causes spurious lease expiry.
4. `service=runner level=error` recent lines for the affected `stream_id`/`shard_id`.

## Steps

- **Graceful restart (preferred):** SIGTERM the machine → the runner finishes the
  current tick, **persists checkpoints, and releases its leases** within the ≤ 30s
  kill_timeout (D-5). A peer immediately acquires the released leases (clean handoff, no
  failover delay).
  - **Prod:** `fly machines restart <runner_machine> -a $FLY_APP` (sends SIGTERM).
  - **Local:** `docker compose -p dataforge restart runner`.
- **Ungraceful kill (crash/OOM):** lease TTL (15s) + checkpoint restore ⇒ takeover
  ≤ 30s total; fencing token strictly increases (`reason=failover`). Pending late
  re-emissions survive (INV-CHA-5).
- **Crash loop on a bad image** → roll back ([deploy-rollback.md](deploy-rollback.md)).
- **One stuck shard** that keeps dying → see
  [StreamFailoverExhausted.md](StreamFailoverExhausted.md) /
  [lease-failover-diagnosis.md](lease-failover-diagnosis.md).

## Verification

- `df_runner_active_leases` stabilizes; every shard is owned by exactly one runner.
- Per-shard `sequence_no` is gapless/monotone across the restart (INV-GEN-7) — no
  canonical gap, no duplicate.
- `df_runner_streams_running` matches expected; tick durations back under the tick
  budget.
- Fresh events for the affected streams become cursor-visible (delivery resumed).
