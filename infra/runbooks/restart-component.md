# Runbook: Per-component restart (index)

How to safely restart each DataForge process group / dependency. The deploy/rollback
invariants are fixed in deployment-architecture §10 (RB-1..RB-9); this index links the
per-component procedures and states the shared safety rules.

## Shared rules

- **Rolling order for a full deploy** (RB-1): `worker → runner → ws → web` (control
  plane first so new runner code never sees an older desired-state schema).
- **Data plane survives restarts** (D-5): runner machines drain on SIGTERM (checkpoint
  + lease release, ≤ 30s kill_timeout); ungraceful kills are covered by lease failover
  (≤ 30s). The ledger write precedes publication (INV-GEN-5) so a restart never loses
  canonical events.
- **Migrations are expand/contract, N−1-compatible** (RB-3) — never a down-migration in
  prod; a bad migration is rolled forward or restored (RB-7).
- **Local (compose)** uses `docker compose -p dataforge ...`; **prod (Fly)** uses
  `fly machines restart <id> -a $FLY_APP` (one group at a time).

## Components

| Component | Runbook | Blast radius of a restart |
|---|---|---|
| `web` (REST API, /metrics) | [restart-web.md](restart-web.md) | none — LB reroutes; 2 machines |
| `ws` (WebSocket fan-out) | [restart-ws.md](restart-ws.md) | none — clients reconnect + resume-from-cursor |
| `worker` (Celery control plane) | [restart-worker.md](restart-worker.md) | delayed commands/schedules; data plane unaffected |
| `beat` (scheduler, inside worker) | [restart-beat.md](restart-beat.md) | scheduled jobs paused; catch-up on resume |
| `runner` (data plane) | [restart-runner.md](restart-runner.md) | leased shards fail over ≤ 30s; no canonical loss |
| `buffer-writer` (Kafka→buffer sink) | [restart-buffer-writer.md](restart-buffer-writer.md) | delivery lag while down; resumes from committed offset |
| `kafka` (single broker) | [restart-kafka.md](restart-kafka.md) | delivery pauses; bounded loss only on volume loss (§9.4) |

## Postgres / Redis (managed dependencies)

- **Postgres**: prod is managed (MPG) — restarts are a managed-platform op; a restore is
  RB-7 / [restore-drill.md](restore-drill.md). Local: `docker compose -p dataforge
  restart postgres`. After any restart, verify `readyz` on `web` and that the runner
  reacquires leases.
- **Redis**: leases/stats/quota/rate-limit live here. Auth/rate-limit/quota paths
  fail-open, so a brief Redis restart degrades but does not hard-fail control plane;
  runners re-acquire leases after a Redis blip. Local: `docker compose -p dataforge
  restart redis`. Prod: managed restart.

After any component restart, confirm: `readyz` healthy for the group, the relevant df_
metrics resume, and zero new ERROR lines for 5 minutes.
