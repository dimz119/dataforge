# Runbook: Restart `beat` (Celery scheduler)

Beat is the periodic-task scheduler. It does **not** run as its own process group — it
runs **inside the `worker` group** under a Redis singleton lock so scaling worker
machines never double-fires (§7.4). "Restarting beat" therefore means restarting (or
re-acquiring the lock within) the worker that hosts it.

## Symptom / when to use

- `df_beat_last_run_timestamp_seconds` is stale (drives [BeatDead.md](BeatDead.md)).
- Scheduled jobs (lease watchdog, partition maintenance, ledger archive, idle
  auto-pause) are not firing.

## Diagnosis

1. `time() - df_beat_last_run_timestamp_seconds{schedule=~".+"}` — all schedules stale
   ⇒ beat is dead; one schedule stale ⇒ that task is failing (not beat itself).
2. Is the worker hosting beat up? `fly status -a $FLY_APP` /
   `docker compose -p dataforge ps worker`.
3. Is the singleton lock held by a dead worker? A crashed holder blocks a new scheduler
   until the lock TTL expires.

## Steps

- **Restart the worker that hosts beat** (this is the primary action):
  see [restart-worker.md](restart-worker.md). On restart, the beat scheduler reacquires
  the singleton lock and resumes ticking.
- **Stuck singleton lock** → after confirming the prior holder is gone, clear the stale
  beat lock key in Redis, then restart the worker so beat takes the lock cleanly.
- Idempotent tasks + `task_acks_late` mean missed runs catch up safely on the next tick
  (e.g. a missed 02:00 ledger archive re-runs and archives the backlog of eligible
  partitions).

## Verification

- Every `schedule` label shows a recent `df_beat_last_run_timestamp_seconds`.
- The 15s lease watchdog, hourly partition maintenance, daily ledger archive, and 5min
  idle auto-pause resume on cadence.
- No unattached-partition write errors (maintenance caught up).
- The [BeatDead.md](BeatDead.md) alert resolves.
