# Runbook: RunnerLeaseTakeoverSpike (PAGE)

The runner fleet is churning leases: `rate(df_runner_lease_takeovers_total[10m]) > 3`
(observability §9). Healthy failover is rare and bounded; a spike means runners are
repeatedly losing and re-acquiring shard leases (instability), not a one-off failover.

- **Source metric:** `df_runner_lease_takeovers_total{reason=failover|first_start}`,
  `df_runner_active_leases`, `df_runner_streams_running`.

## Symptom

Leases are being taken over far more often than normal. Each takeover restarts a
shard's worker from its last checkpoint — correct (no canonical gaps, fencing tokens
strictly increase) but expensive, and a sustained spike indicates a sick runner, Redis
instability, or a crash loop. The `df-runner-generation` dashboard shows takeovers
spiking and `df_runner_active_leases` oscillating.

## Diagnosis

1. Split by reason:
   ```
   sum by (reason) (rate(df_runner_lease_takeovers_total[10m]))
   ```
   - `failover` dominant → leases are expiring (a runner is dying/heartbeat-starved or
     Redis is flaky).
   - `first_start` dominant → many streams starting at once (expected during a fleet
     bring-up; not usually a problem unless it loops).
2. Are runner machines flapping? `fly status -a $FLY_APP` (restart counts), `fly logs`.
   Local: `docker compose -p dataforge ps runner` + logs. Look for OOM/crash loops.
3. Is Redis (the lease store) healthy? Lease heartbeats write to Redis; Redis latency
   or eviction causes spurious lease expiry → takeover storms.
4. Correlate with a recent deploy (a bad runner image crash-loops → endless failover) →
   [deploy-rollback.md](deploy-rollback.md).
5. Check `df_runner_tick_overruns_total` / `df_runner_tick_duration_seconds` — a runner
   pegged so hard it misses heartbeats will lose its own leases.

## Steps

- **A specific runner machine is crash-looping** → stop/replace it so it stops
  reacquiring and re-dying; healthy peers hold the shards. See
  [restart-runner.md](restart-runner.md) and
  [lease-failover-diagnosis.md](lease-failover-diagnosis.md).
- **Redis degraded** → restart Redis; leases stabilize once heartbeats land reliably.
- **Bad deploy crash loop** → roll back the runner image
  ([deploy-rollback.md](deploy-rollback.md)).
- **Tick overruns starving heartbeats** → reduce per-runner shard load (rebalance) or
  scale the runner group.
- If a stream has exhausted failover and entered `failed`, that is
  [StreamFailoverExhausted.md](StreamFailoverExhausted.md).

## Verification

- `rate(df_runner_lease_takeovers_total[10m])` drops back to ≤ 3 and stays there.
- `df_runner_active_leases` is stable (no oscillation); `df_runner_streams_running`
  matches expected.
- No canonical gaps on affected streams (per-shard `sequence_no` gapless, INV-GEN-7).
- The alert resolves once the takeover rate is below threshold for the hold window.
