# ADR-0006 — Celery is control plane only; leased runner processes are the data plane

**Deliverable:** D17

Celery handles lifecycle commands, scheduled jobs, and batch work; continuous event generation runs in supervised, long-lived **stream-runner processes** that acquire streams via Redis leases and reconcile toward control-plane desired state every tick. This split warranted an ADR because the obvious default — "Celery is in the stack, run generation in Celery" — caps throughput in the low thousands of TPS and fights Celery's execution model, while the split decided here scales by adding processes and keeps every component doing what it is built for.

- **Status:** Accepted
- **Date:** 2026-06-10
- **Decides for:** the runtime topology of the data plane (D12); stream lifecycle mechanics; crash-safety and scaling model; Phases 5–11

## Context

The forces:

- **Generation is a long-lived, stateful, high-frequency loop.** A stream runs for hours or days at up to 1,000 TPS (MVP cap, PRD §7), holding hot actor/session state machines, dwell timers, and entity-pool working sets whose locality determines throughput (ADR-0007). The 100k-TPS aggregate target (D15) demands that per-event overhead be nanoseconds-to-microseconds, not a task dispatch.
- **Celery is mandated** in the stack and excellent at what it is designed for: finite, idempotent, queue-dispatched tasks. It is explicitly *not* designed for indefinite loops — time limits, warm shutdown, worker recycling, and prefetch all assume tasks end.
- **Control requirements are reconciliation-shaped:** start/pause/resume/stop with idempotent semantics (INV-STR-3), dynamic TPS effective within 2 s, chaos toggles at runtime, pause that survives via checkpoints, crash failover under 30 s (Phase 5/6 exit criteria). These are "converge actual state toward desired state" problems, not "execute command" problems.
- **Crash safety and scaling must share one model:** the requirements demand horizontal scaling without re-architecture, so the unit of failover and the unit of parallelism should be the same thing.

## Decision

1. **Celery (Redis broker) is the control plane**: workspace/stream lifecycle command orchestration, scheduled schema-evolution triggers ("evolve to v2 at T+x", Phase 10), periodic entity-pool snapshots, Celery-backed batch/backfill generation jobs (Phase 4), and cleanup/retention jobs (buffer partition drops, ledger retention). Celery workers never generate streaming events. In the ubiquitous language, *worker* means Celery control-plane worker only; the data-plane process is a *runner* (domain model §6.3).
2. **Runners are the data plane**: long-lived supervised processes (their own process group in Compose and on Fly per ADR-0015) that:
   - **acquire work via Redis leases** — one lease per `(stream_id, shard_id)`, heartbeat every 5 s, TTL 15 s, atomically acquired, with fencing tokens so a runner that loses its lease stops emitting before the new holder's first tick (INV-STR-2; fencing design owned by [../02-architecture/backend-architecture.md](../02-architecture/backend-architecture.md));
   - **poll the control-plane desired-state document every tick and reconcile**: run-state (`running`/`paused`/`stopped`), `target_tps` (token-bucket pacing), chaos configuration, schema-upgrade schedule (domain model §4.1, §4.4);
   - run the behavior and chaos engines and publish to internal Kafka (ADR-0005).
3. **Actors shard by `partition_key` hash** across a stream's shards, so one actor's state lives in exactly one runner's memory — locality by construction. MVP: 1 shard per stream; Phase 11 introduces N shards per stream as additional leases — **scaling and failover are the same mechanism**.
4. **Crash safety = lease failover + checkpoint restore.** Runners checkpoint shard state (actor/session machines, timers, RNG positions, virtual-clock position, last `sequence_no`) every 30 s and on pause/stop. A runner crash lets the lease expire (≤ 15 s detection); another runner claims it and restores from the latest checkpoint within 30 s total, with the stream remaining `running` (domain model §4.3). Determinism (ADR-0008) makes restored generation continue the identical sequence.
5. **Users never address runners.** All user interaction is desired-state mutation on the control-plane API (INV-STR-4); Stream Control owns *what should be running*, Generation owns *running it*.

The division of labor, exhaustively:

| Work | Executes on | Why |
|---|---|---|
| Stream lifecycle commands, desired-state writes | DRF API + Celery worker | Low-volume, strongly consistent, queue-shaped |
| Scheduled schema evolution, pool snapshots, cleanup/retention, batch/backfill jobs | Celery worker | Finite, idempotent, schedulable — Celery's home turf |
| Continuous event generation, chaos transform, Kafka publish | Runner (leased per shard) | Long-lived, stateful, latency-sensitive loop |
| Failover and horizontal scaling | Lease expiry / additional shard leases | One mechanism for both (this ADR §4) |

## Alternatives considered

- **Celery time-sliced tick tasks** — panel position P2: each task generates one time slice of events, then re-enqueues itself. Rejected per the resolved disagreement: per-task dispatch overhead (broker round-trip, serialization, prefetch) caps aggregate throughput in the low thousands of TPS — P2's own stated ceiling; actor state must round-trip Redis on every slice, destroying the locality the behavior engine depends on; and pacing jitters with queue depth, making smooth token-bucket TPS control impossible.
- **Celery-managed long-running loop tasks holding leases** — panel position P1: keep generation inside Celery but as indefinitely running tasks. Rejected: indefinite tasks fight `time_limit`/`soft_time_limit`, warm-shutdown semantics, and worker recycling (`max-tasks-per-child`) — operational friction P1's own risk list names; every deploy or worker restart would interrupt streams through machinery never designed to drain a stateful loop gracefully. P1's *reconciliation* idea was correct and is adopted; its *process model* is not.
- **The adopted synthesis** is explicitly P3's dedicated-process model fused with P1's desired-state reconciliation — recorded here because neither panel position alone was taken.
- **One OS process per stream** (spawn/supervise a process per started stream). Rejected: process count scales with stream count — thousands of classroom streams would mean thousands of processes with poor packing and slow start; a fixed pool of runner processes leasing many shards each packs hot streams densely and starts streams in milliseconds. The lease model subsumes this alternative (a runner *may* hold one lease).
- **No Celery at all** (runners + cron). Rejected: Celery is mandated, and scheduled evolution, batch jobs, snapshots, and cleanup genuinely are queue-shaped work; removing it would reinvent it badly inside runners.

## Consequences

### Positive

- Throughput scales with processes and shard leases, not task-dispatch rates; the Phase-11 staircase (D15) adds leases and partitions, never a new execution model.
- Reconciliation makes the control plane naturally idempotent and self-healing: lost commands, crashed runners, and restarts all resolve as "converge again" (INV-STR-1/3).
- Clean operational vocabulary and ownership: Celery worker problems and runner problems are different dashboards, different runbooks ([../02-architecture/observability.md](../02-architecture/observability.md)).

### Negative

- A second runtime to operate: runner supervision, graceful lease drain on deploy, and process-group sizing are new responsibilities ([../02-architecture/deployment-architecture.md](../02-architecture/deployment-architecture.md) owns the topology and deploy choreography).
- Lease correctness is subtle — split-brain prevention requires fencing tokens and disciplined heartbeat handling; this is the riskiest code in the platform and gets a dedicated kill-test in the Phase 5 exit criteria (failover < 30 s with documented semantics).
- Desired-state polling adds a small per-tick Redis read per shard; bounded by batching reads per runner per tick.

### Follow-ups

- [../02-architecture/backend-architecture.md](../02-architecture/backend-architecture.md): lease/fencing implementation, desired-state document shape, runner internal architecture.
- [../02-architecture/scaling-strategy.md](../02-architecture/scaling-strategy.md): per-runner event ceiling and shards-per-stream arithmetic (the D15 staircase).
- Phase 5 implements single-shard runners with kill-test exit criteria; Phase 6 adds checkpointed pause/resume and dynamic TPS; Phase 11 adds N-shard streams.
