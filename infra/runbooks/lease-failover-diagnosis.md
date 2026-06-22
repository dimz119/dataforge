# Incident runbook: Lease-failover diagnosis

Diagnose runner shard-lease behavior — distinguish **healthy failover** (a rare,
correct event) from a **failover storm** (instability) or a **stuck shard** that cannot
recover. Per-shard leases live in Redis with fencing tokens; failover is the data
plane's resilience mechanism (≤ 30s, domain-model §4.3), exercised by the Phase 5/6
OPS-1/2 kill-tests.

## How leases work (context)

- Each `(stream_id, shard_id)` holds one Redis lease with a fencing token. The owning
  runner heartbeats it; on heartbeat loss the lease expires (TTL 15s) and a peer
  acquires it with a **strictly greater** fencing token.
- `df_runner_lease_takeovers_total{reason=failover}` increments on takeover
  (`reason=first_start` for an initial acquire). `df_runner_active_leases` is the live
  held-shard count (set by the supervisor).
- Takeover restarts the shard's worker from its last checkpoint; the arrival-cursor
  rebase makes continuation byte-identical, so there is **no canonical gap and no
  duplication** across the failover boundary (INV-GEN-7).

## Symptom triage

| Observation | Likely cause | Go to |
|---|---|---|
| One takeover, then stable | Healthy failover (a machine restarted) | nothing — verify gaplessness |
| `rate(takeovers[10m]) > 3`, oscillating leases | Failover storm (sick runner / flaky Redis / crash loop) | below |
| A stream → `failed` after repeated attempts | Stuck shard, failover exhausted | [StreamFailoverExhausted.md](StreamFailoverExhausted.md) |

## Diagnosis (storm)

1. Split takeovers by reason:
   `sum by (reason) (rate(df_runner_lease_takeovers_total[10m]))`.
2. Are runner machines flapping (OOM/crash)? `fly status` / `docker compose ps runner`
   restart counts + `fly logs` for OOM.
3. Redis health — lease heartbeats write to Redis; Redis latency/eviction → spurious
   expiry → takeover storm. Check Redis CPU/memory/evictions.
4. Tick overruns starving heartbeats:
   `rate(df_runner_tick_overruns_total[5m])`, `df_runner_tick_duration_seconds` p95 vs
   `RUNNER_TICK_MS`. A runner pegged hard misses its own heartbeats and loses leases.
5. Correlate with a recent deploy (`fly releases`) — a bad runner image crash-loops.

## Steps

- **Sick/crash-looping runner machine** → stop/replace it so it stops re-acquiring and
  re-dying; healthy peers hold the shards ([restart-runner.md](restart-runner.md)).
- **Flaky Redis** → stabilize/restart Redis; leases settle once heartbeats land
  reliably.
- **Bad deploy** → roll back ([deploy-rollback.md](deploy-rollback.md)).
- **Tick overruns** → reduce per-runner shard load (rebalance) or scale the runner
  group so heartbeats keep up.
- **Stuck shard / exhausted** → [StreamFailoverExhausted.md](StreamFailoverExhausted.md)
  (poison checkpoint rollback, dependency restore).

## Verification

- `rate(df_runner_lease_takeovers_total[10m]) ≤ 3`; `df_runner_active_leases` stable.
- Each shard owned by exactly one runner; fencing tokens strictly increasing only on
  real takeovers.
- Per-shard `sequence_no` gapless/monotone across every failover boundary (spot-check
  the affected streams) — no gap, no duplicate.
- Delivery resumes (fresh events cursor-visible) for the affected streams.
