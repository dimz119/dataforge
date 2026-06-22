# Runbook: BeatDead (PAGE)

The Celery beat scheduler is dead: `time() - max(df_beat_last_run_timestamp_seconds)`
exceeds 2× the tightest schedule (> ~120s, observability §9). No scheduled job is
firing — lease watchdog, partition maintenance, ledger archive, idle auto-pause, and
quota/retention jobs are all stalled.

- **Source metric:** `df_beat_last_run_timestamp_seconds{schedule}` (gauge, set on each
  beat tick).

## Symptom

Beat has not ticked recently. Downstream effects accumulate silently:
- **Lease watchdog** (15s) not running → stuck stream starts not failed within the
  window.
- **Buffer/ledger partition maintenance** not running → the create-ahead window stops
  rolling; eventually a write hits an unattached partition (loud failure, §8.1).
- **Ledger archive** (daily 02:00) not running → ledger hot tier grows unbounded.
- **Idle auto-pause** (5min) not running → idle streams keep consuming.

## Diagnosis

1. Confirm which schedules are stale:
   ```
   time() - df_beat_last_run_timestamp_seconds{schedule=~".+"}
   ```
   All stale → beat itself is dead. One stale → that specific task is failing (not beat).
2. Beat runs inside the `worker` group under a Redis singleton lock (§7.4). Check:
   - The `worker` machine is up: `fly status -a $FLY_APP` / `docker compose -p dataforge
     ps worker`.
   - The beat process is running inside it (the worker hosts beat).
   - The Redis singleton lock is not stuck held by a dead worker (a crashed worker that
     held the lock blocks a new one from scheduling until the lock TTL expires).
3. Check worker logs for a beat crash/exception: `service=worker` ERROR lines around the
   last good `df_beat_last_run_timestamp_seconds`.

## Steps

- **Worker/beat process down** → restart the `worker` group
  ([restart-worker.md](restart-worker.md)). Beat resumes; `task_acks_late` +
  idempotent tasks mean missed runs are safely re-attempted (delayed catch-up on the
  next tick).
- **Stuck singleton lock** → clear the stale beat lock in Redis (the documented key)
  once the dead holder is confirmed gone, then restart the worker so beat reacquires it.
- **Beat crashing on a bad schedule entry** → fix the schedule/config and redeploy;
  roll back if a recent deploy introduced it ([deploy-rollback.md](deploy-rollback.md)).
- After recovery, verify the catch-up jobs ran (especially partition maintenance — see
  Verification).

## Verification

- `time() - max(df_beat_last_run_timestamp_seconds)` returns under the threshold.
- Each `schedule` label shows a recent run timestamp.
- Partition maintenance created the current/next windows (no unattached-partition write
  errors); the ledger archive caught up if 02:00 was missed.
- The lease watchdog, idle auto-pause, and quota jobs resume on cadence.
- The alert resolves once beat ticks within the hold window.
