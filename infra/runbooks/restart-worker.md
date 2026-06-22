# Runbook: Restart `worker` (Celery control plane)

The `worker` process group runs the five control-plane queues (`control`, `lifecycle`,
`validation`, `exports`, `maintenance`) and **hosts the beat scheduler** under a Redis
singleton lock (§7.4). It is control plane only (ADR-0006) — the data plane (runner) is
unaffected by a worker restart. One machine in prod (Fly auto-restart ≤ minutes).

## Symptom / when to use

- Commands/jobs not processing; queue depth climbing
  (`df_celery_queue_depth{queue}`, drives `CeleryQueueBacklog`).
- Beat is dead (drives [BeatDead.md](BeatDead.md)) — restarting the worker restarts beat.
- After a deploy (worker restarts first in the rolling order, RB-1).

## Diagnosis

1. `fly status -a $FLY_APP` / `docker compose -p dataforge ps worker`.
2. `df_celery_queue_depth{queue}` per queue (which queue is backed up),
   `df_celery_tasks_total{task,state}` (failures),
   `df_celery_task_duration_seconds{task}`.
3. `df_beat_last_run_timestamp_seconds{schedule}` — is beat ticking?
4. `service=worker level=error` recent lines; check the Redis singleton lock is not
   stuck held by a dead worker.

## Steps

- **Prod:** `fly machines restart <worker_machine> -a $FLY_APP`.
- **Local:** `docker compose -p dataforge restart worker`.

`task_acks_late=True` + every task idempotent → in-flight tasks re-deliver and re-run
safely; missed beat schedules catch up on the next tick. If the singleton beat lock is
stuck, clear the stale lock key in Redis only after confirming the prior holder is gone,
then restart so beat reacquires it.

## Verification

- `df_celery_queue_depth{queue}` drains back to baseline.
- `df_beat_last_run_timestamp_seconds` advances for every `schedule` label.
- `df_celery_tasks_total{state="SUCCESS"}` resumes; no rising `FAILURE`.
- Lifecycle commands (pause/fail), partition maintenance, and idle auto-pause run on
  cadence.
