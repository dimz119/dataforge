# Incident runbook: Quota incident

A workspace has hit a quota (events/day, per-stream/aggregate TPS, concurrent
backfills) or the platform admission ceiling, and either (a) a customer is asking why
their stream paused, or (b) admission is rejecting starts platform-wide. Quotas
**never delete data** (INV-TEN-5) — exhaustion pauses, it does not destroy.

## Quota model (PRD §7 / scaling-strategy §5)

| Quota | Free | Classroom | Pro | Enforced |
|---|---|---|---|---|
| Per-stream TPS | 50 | 100 | 1000 | command time (start / TPS-raise) |
| Aggregate workspace TPS | 100 | 1000 | 2000 | command time |
| Events/day | 1M (Free) | tier value | tier value | metering (Redis day-bucket) → `paused_quota` |
| WS connections | 10 | 100 | 50 | WS connect |
| Concurrent backfills | 2 | 2 | 2 | backfill create |
| Shards/stream | ≤ 64 (platform) | ≤ 64 | ≤ 64 | create |

**Platform admission:** Σ provisioned `target_tps` ≤ 70% of measured capacity
(3,500 eps at the 5k GA ceiling) → otherwise `503 + Retry-After: 300` on start/TPS-raise
(running streams are never touched).

## Symptom triage

| Observation | Cause | Section |
|---|---|---|
| One stream `paused_quota`, `df_quota_pauses_total{reason="quota"}` bumped | events/day exhausted | A |
| One stream `paused_idle`, audit `system_paused{reason="idle"}` | idle auto-pause (5min job) | B |
| Starts/TPS-raises returning 503 with Retry-After | platform admission ceiling | C |
| `429 rate-limited` + `df_rate_limited_total{scope}` | per-key rate limit (not a quota) | D |
| `QuotaPauseSpike` alert | many streams pausing at once | A + capacity review |

## A. Events/day exhaustion (`paused_quota`)

1. Confirm: `GET /streams/{id}` → `status: paused_quota`; the console QuotaMeter shows
   100%. `df_quota_pauses_total{reason="quota"}` incremented.
2. Data is intact (the ledger/buffer are untouched). Explain to the customer this is the
   daily cap, not data loss.
3. **Resume is guarded (T7):** resume is rejected until there is day-headroom (the meter
   must show consumption below the cap for the current UTC day). It resets at UTC
   midnight; a tier upgrade (operator-side) raises the cap immediately.
4. To resume now: raise the workspace's plan/quota (operator), or wait for the UTC-day
   rollover, then one-click resume.

## B. Idle auto-pause (`paused_idle`)

1. The 5-min `streams.idle_auto_pause` job paused a stream idle past
   `idle_pause_minutes`; audit `streams.stream.system_paused{reason="idle"}` recorded.
2. This is expected cost control. **One-click resume** from the console (no headroom
   guard — idle pause is not a quota breach).

## C. Platform admission ceiling (503)

1. Σ provisioned `target_tps` across running streams is at the 70% capacity budget.
   `streams.application.metering.admission_budget_eps()` is the budget;
   `check_admission` raises `AdmissionDenied` → 503 + Retry-After: 300.
2. Running streams are unaffected — only **new** starts / TPS-raises are deferred.
3. Options: wait for provisioned load to drop (streams stop/pause), or raise platform
   capacity (`DF_ADMISSION_CAPACITY_EPS`) if real measured headroom exists (verify
   against the LOAD harness first — do not inflate the budget past measured capacity).

## D. Per-key rate limit (429) — not a quota

- Per-key token buckets: data-events 600/min, control 120/min, lifecycle 30/min, WS
  connect 10/min. A 429 with RFC-9457 `rate-limited` + Retry-After is the client sending
  too fast on one key; `df_rate_limited_total{scope}` shows which scope. Advise the
  client to back off / use batch reads (`limit` up to 1000). Buckets are per-key
  isolated and fail-open if Redis is down.

## Verification

- Resumed stream returns to `running`; QuotaMeter reflects current usage; data intact.
- `df_quota_pauses_total` stops climbing; `QuotaPauseSpike` resolves.
- Admission 503s stop once provisioned load is under budget.
- One workspace's metering never moved another's counters (INV-OBS-3 isolation holds).
