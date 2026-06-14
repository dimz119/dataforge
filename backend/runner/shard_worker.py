"""Shard worker — the reconciliation tick (backend-architecture §8.3).

One shard worker = one asyncio task driving one (stream, shard) toward its desired
state at a fixed 1,000 ms cadence. The supervisor (§8.1) owns the lease, the
heartbeat, and admission control; this module owns the *normative* §8.3 tick:

    acquire lease (+ fencing token)        ── done by the supervisor; handed in
    load pin + LRU IR + checkpoint         ── §8.3 setup
    report lifecycle running (T3)          ── runner-converged column
    loop @1000ms:
      1. poll desired (batched read)
      2. reconcile lifecycle (stopped→finalize T10 / paused→T6 / resume→T7→T8)
      3. pacing  rate = target_tps / shard_count  + engine live TPS (BE-P2)
      4. generate (paced budget ≤ 500, until = tick-end)
      5. ledger.append  BEFORE chaos      (INV-GEN-5: ledger is clean truth)
      6. chaos.transform  IDENTITY        (Phase 9 pass-through slot)
         injections / late_buffer         (Phase 9 pass-through slots)
      7. publisher.publish keyed by partition_key
      8. stats.incr  (Redis counters)
      9. checkpoint every 30 s  (fenced conditional write → FencingError on stale)
      sleep_to_next_tick

Pipeline-order guarantees as code positions: ledger append precedes chaos (5 < 6);
checkpoint captures RNG cursors + clock + last sequence_no so a failover replays at
most 30 s of *deterministic* generation into the idempotent ledger/Kafka sinks
(§8.5).

Pause/resume (Phase 6, the real T6/T7/T8):

* **Pause (T5→T6):** desired ``paused`` → halt emission within ONE tick, persist a
  checkpoint SYNCHRONOUSLY before reporting ``paused``, RETAIN the lease, freeze the
  virtual clock at the frontier ``F``, and idle-poll desired. The warm engine state
  stays in memory (a paused stream keeps its holder + state); no restore on resume.
* **Resume (T7→T8):** desired ``running`` again while paused → re-anchor the virtual
  clock at ``(wall_now, F)`` (dwell timers rebase automatically — they store absolute
  virtual due-times, §9.3 step 4), report ``running``, and continue ticking. In-flight
  funnels continue with ZERO ``sequence_no`` gaps (the counter never left memory).
* **Stop-override (T9):** a desired ``stopped`` seen while pausing/paused/running wins
  — finalize (T10) regardless of the prior pause intent.

Dynamic TPS (§3.6, ≤ 2 s): each running tick reads ``desired.target_tps`` into both
the token bucket (wall rate) and the engine's live TPS slot (arrival density, BE-P2),
so a change is effective within one poll + one pacing adjustment. The recorded value
is the determinism input (BE-P4): replays of the same stream read the same schedule.

The engine stays pure: this host compiles the IR, builds a *live* shard, injects
the ledger/publisher/checkpoint adapters, and drives ``Shard.generate``. No Django
import reaches ``dataforge_engine`` (import-linter contract 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from dataforge_engine.behavior import (
    Shard,
    ShardConfig,
    compile_manifest_cached,
)
from dataforge_engine.behavior.scheduler import TokenBucket
from generation.infra.clock import SystemWallClock
from runner import lifecycle
from runner.checkpoint_store import CheckpointStore
from runner.fencing import FencingError
from streams.application import desired_state
from streams.domain.models import RUN_PAUSED, RUN_STOPPED

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from dataforge_engine.behavior import ManifestIR
    from runner.leases import Lease
    from runner.publisher import EventPublisher
    from streams.application.desired_state import DesiredState

logger = structlog.get_logger("dataforge.runner.shard")

__all__ = ["ShardWorker"]

# §8.3 constants.
TICK_MS = 1000
GENERATE_BUDGET_MAX = 500  # paced budget cap per tick (§8.3 step 4)
CHECKPOINT_INTERVAL_S = 30.0  # §8.4 periodic cadence
LATE_BUFFER_TAKE_LIMIT = 500  # Phase 9 pass-through (no due re-emissions yet)


def _deterministic_emitted_at(batch: list[Any]) -> list[Any]:
    """Make each event's ``emitted_at`` deterministic by pinning it to ``occurred_at``.

    §8.5 failover idempotency: a takeover regenerates the gap and re-appends to the
    ledger, which dedups on ``(stream_id, shard_id, sequence_no, emitted_at)`` (the
    partition key ``emitted_at`` is forced into the unique constraint, database-schema
    §6.1). The engine stamps ``emitted_at`` from the injected SYSTEM wall clock, so
    the killed holder and the takeover holder would stamp the SAME sequence_no with
    DIFFERENT wall times → the ON CONFLICT misses → duplicate rows. ``occurred_at`` is
    the deterministic simulated instant (``instant_for(virtual_due_at)``), fixed by
    pin + seed + sequence (INV-GEN-3) and restored exactly from the checkpoint, so
    pinning ``emitted_at`` to it makes regeneration byte-identical and the ledger +
    REST cursor replay idempotent. ``emitted_at`` is a wall field that "carries no
    business meaning and is near-constant" (event-model §164), and in live mode at
    speed 1.0 ``occurred_at`` tracks real time, so the stop-latency budget still holds.
    """
    for env in batch:
        occurred = env.get("occurred_at") if hasattr(env, "get") else None
        if occurred is not None:
            env["emitted_at"] = occurred
    return batch


def _config_sha256(merged_config: dict[str, Any]) -> str:
    """SHA-256 of the canonical merged config (the IR cache discriminator)."""
    canonical = json.dumps(merged_config, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ShardWorker:
    """Drives one (stream, shard) through the §8.3 reconciliation tick.

    Constructed by the supervisor with the acquired :class:`~runner.leases.Lease`
    (its fencing token is carried into every durable write) and the shared Redis +
    a built :class:`~runner.publisher.EventPublisher`. ``run`` is the supervised
    asyncio task body; the supervisor cancels it when the lease is lost (§8.2).
    """

    def __init__(
        self,
        *,
        lease: Lease,
        publisher: EventPublisher,
        redis: Redis,
        tick_ms: int = TICK_MS,
        wall_clock: Any | None = None,
    ) -> None:
        self._lease = lease
        self._stream_id = lease.shard.stream_id
        self._shard_id = lease.shard.shard_id
        self._fencing_token = lease.fencing_token
        self._publisher = publisher
        self._redis = redis
        self._tick_s = tick_ms / 1000.0
        self._wall = wall_clock or SystemWallClock()
        # Set during setup.
        self._workspace_id: str = ""
        self._shard: Shard | None = None
        self._ledger: Any | None = None
        self._checkpoints: CheckpointStore | None = None
        self._bucket: TokenBucket | None = None
        self._config_sha256: str = ""
        self._shard_count: int = 1
        self._checkpoint_seq: int = 0
        self._last_checkpoint_at: float = 0.0
        # Pause/resume bookkeeping (T6/T8). ``_paused`` is the warm-hold flag: while
        # set, the worker idles (lease retained, emission halted, clock frozen). It is
        # entered once per pause (synchronous checkpoint on entry) and cleared on the
        # resume transition (clock re-anchored, lifecycle reported running again).
        self._paused: bool = False
        self.ticks = 0
        self.emitted_total = 0

    # -- run (the supervised task body) ------------------------------------------

    async def run(self) -> None:
        """Set up, report running (T3), then loop the tick until stopped/cancelled.

        Cancellation (lease lost, §8.2) propagates out of the tick between pipeline
        steps; the supervisor handles teardown. A :class:`FencingError` from the
        checkpoint conditional write means a takeover happened — the worker stops
        immediately (a zombie writes zero rows post-takeover).
        """
        await self._setup()
        await lifecycle.report_lifecycle(
            self._stream_id, lifecycle.RUNNING, workspace_id=self._workspace_id
        )
        logger.info(
            "shard.running",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            fencing_token=self._fencing_token,
        )
        try:
            while True:
                stop = await self._tick()
                if stop:
                    return
                await self._sleep_to_next_tick()
        except FencingError:
            logger.warning(
                "shard.fenced",
                stream_id=self._stream_id,
                shard_id=self._shard_id,
                fencing_token=self._fencing_token,
            )
            raise

    # -- setup (§8.3: load pin + LRU IR + checkpoint, build live shard) -----------

    async def _setup(self) -> None:
        """Load the pin, compile the LRU-cached IR, restore checkpoint or seed.

        On takeover (a checkpoint exists) the engine is rehydrated and the gap is
        regenerated into the idempotent sinks on the first ticks (§8.5). On first
        start (no checkpoint) the shard seeds its head ``op:"r"`` snapshot rows.
        """
        desired = await asyncio.to_thread(desired_state.desired_for, self._stream_id)
        if desired is None:
            raise RuntimeError(f"stream {self._stream_id} vanished before setup (§8.3)")
        self._workspace_id = str(desired.workspace_id)
        self._shard_count = max(1, desired.shard_count)
        self._config_sha256 = _config_sha256(desired.pinned_config)
        ir = compile_manifest_cached(
            desired.pinned_config,
            config_sha256=self._config_sha256,
            schema_versions={},
        )
        self._shard = self._build_shard(ir, desired)
        self._ledger = self._build_ledger(desired)
        self._checkpoints = CheckpointStore(
            workspace_id=str(desired.workspace_id),
            stream_id=self._stream_id,
            shard_id=self._shard_id,
        )
        self._bucket = TokenBucket(
            rate_per_second=desired.target_tps / self._shard_count,
            now=self._wall.now(),
        )
        restored = await self._checkpoints.load()
        if restored is not None:
            # §8.5 takeover: rehydrate pools/RNG/clock and resume the sequence.
            await self._checkpoints.restore_into(self._shard, restored)
            self._checkpoint_seq = restored.checkpoint_seq
            logger.info(
                "shard.restored",
                stream_id=self._stream_id,
                shard_id=self._shard_id,
                checkpoint_seq=restored.checkpoint_seq,
                last_sequence_no=restored.last_sequence_no,
            )
        else:
            # First start: seed head snapshots, append them BEFORE chaos (INV-GEN-5),
            # publish them so consumers see the catalog, then begin the tick loop.
            head = _deterministic_emitted_at(self._shard.seed())
            if head:
                await asyncio.to_thread(self._append_ledger, head)
                self._publisher.publish(head)
                await lifecycle.incr_emitted(self._redis, self._stream_id, len(head))
        self._last_checkpoint_at = time.monotonic()

    def _build_shard(self, ir: ManifestIR, desired: DesiredState) -> Shard:
        """Build a *live*-mode Shard for this (stream, shard) from the pin."""
        config = ShardConfig(
            seed=desired.seed,
            workspace_id=str(desired.workspace_id),
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            virtual_epoch=desired.virtual_epoch,
            speed_multiplier=float(desired.speed_multiplier),
            shard_count=self._shard_count,
            mode="live",
            target_tps=float(desired.target_tps),
        )
        return Shard(ir, config, self._wall)

    def _build_ledger(self, desired: DesiredState) -> Any:
        from generation.infra.ledger_sink import LedgerSink

        return LedgerSink(workspace_id=str(desired.workspace_id))

    # -- the tick ----------------------------------------------------------------

    async def _tick(self) -> bool:
        """One §8.3 reconciliation tick. Returns ``True`` when the worker must stop.

        Steps 1-9 in normative order. A ``stopped`` desired state finalizes (T10) and
        returns ``True`` — and it wins over an in-flight pause/resume (T9 stop-override).
        A ``paused`` desired state halts emission within one tick, checkpoints
        synchronously, retains the lease, freezes the clock, and idles (T6). A
        ``running`` desired state seen while paused resumes (T7→T8): re-anchor the
        clock, report ``running``, continue with zero ``sequence_no`` gaps.
        """
        assert self._shard is not None
        assert self._ledger is not None
        assert self._bucket is not None
        self.ticks += 1

        # 1. poll desired (one batched Postgres read per process per tick; here the
        #    single-shard MVP reads its own row — the supervisor batches in Phase 11).
        desired = await asyncio.to_thread(desired_state.desired_for, self._stream_id)

        # 2. reconcile lifecycle. STOP wins over everything, including an in-flight
        #    pause/resume (T9 stop-override): a stopped (or vanished) stream finalizes
        #    regardless of the warm-hold flag.
        if desired is None or desired.run_state == RUN_STOPPED:
            await self._finalize()
            return True

        if desired.run_state == RUN_PAUSED:
            # T5→T6: halt in one tick, synchronous checkpoint before reporting paused,
            # retain the lease, freeze the clock, idle-poll. Warm state stays in memory.
            await self._enter_paused()
            return False

        # desired == running.
        if self._paused:
            # T7→T8 resume: re-anchor the clock (dwell rebase, §9.3 step 4), report
            # running again, continue. The engine state never left memory.
            await self._resume_from_paused()

        # 3. pacing + live params (§8.3 step 3; dynamic TPS BE-P2, ≤ 2 s).
        #    The bucket adopts the new wall rate AND the engine adopts the new arrival
        #    density this same poll — the recorded TPS value is the determinism input.
        rate = desired.target_tps / self._shard_count
        self._bucket.set_rate(rate)
        self._shard.set_target_tps(float(desired.target_tps))
        # chaos.configure(desired.chaos) is a Phase 9 no-op (identity slot).  # Phase 9

        # 4. generate (paced).
        out = await self._generate_tick()

        # 5-7. ledger BEFORE chaos, chaos identity pass-through, publish.
        published = await self._emit(out)

        # 8. stats.incr (Redis counters, INV-OBS-2).
        await lifecycle.incr_emitted(self._redis, self._stream_id, published)
        self.emitted_total += published

        # 9. checkpoint every 30 s (fenced; raises FencingError on a stale token).
        await self._maybe_checkpoint()
        return False

    async def _generate_tick(self) -> list[Any]:
        """Step 4: paced generation up to the tick-end virtual instant.

        Budget = whole tokens available this tick, capped at ``GENERATE_BUDGET_MAX``
        (§8.3). ``until_us`` is virtual-now (live mode), so the engine processes
        every timer due by now, bounded by the budget. Backfill never runs here.
        """
        assert self._shard is not None and self._bucket is not None
        now = self._wall.now()
        self._bucket.refill(now)
        budget = min(self._bucket.grant(now), GENERATE_BUDGET_MAX)
        if budget <= 0:
            return []
        until_us = self._shard.clock.virtual_now_us(now)
        batch = self._shard.generate(budget, until_us)
        if batch:
            self._bucket.consume(len(batch))
        return _deterministic_emitted_at(batch)

    async def _emit(self, batch: list[Any]) -> int:
        """Steps 5-7: ledger append (BEFORE chaos), chaos identity, publish.

        INV-GEN-5: the ledger sees the clean batch before any downstream stage. In
        Phase 5 chaos is an identity pass-through and injections/late_buffer are
        no-ops — the SEAMS exist so Phase 9 inserts a transform, not a topology
        change.  # Phase 9
        """
        assert self._shard is not None and self._ledger is not None
        if not batch:
            return 0
        # 5. durable BEFORE chaos reads it (INV-GEN-5).
        await asyncio.to_thread(self._append_ledger, batch)
        # 6. chaos.transform — IDENTITY PASS-THROUGH (Phase 9 inserts the transform).
        out = batch  # chaos.transform(batch) → (out, injections, late)  # Phase 9
        # injections.record(injections) / late_buffer.schedule(late)  # Phase 9
        # out += late_buffer.take_due(now, limit=LATE_BUFFER_TAKE_LIMIT)  # Phase 9
        # 7. publish the post-chaos batch, keyed by partition_key (bounded ≤1 batch
        #    in-flight after a lease loss, §8.2 Kafka row).
        return self._publisher.publish(out)

    def _append_ledger(self, batch: list[Any]) -> None:
        """Workspace-armed ledger append (the §4.2 RLS arming for the data plane).

        The runner runs as the NOBYPASSRLS runtime role; the ledger INSERT's Class T
        WITH CHECK requires the row's workspace armed as ``app.workspace_id``. The
        LedgerSink keeps the determinism contract (it does the SQL); this seam supplies
        the tenant context (the worker knows it from the desired-state pin). Runs in a
        worker thread (``to_thread``) so the ``SET LOCAL`` GUC lives on the same
        connection/transaction as the INSERT.
        """
        import uuid as _uuid

        from tenancy.application.services import worker_workspace_scope

        assert self._ledger is not None
        if not self._workspace_id:
            # No setup (unit test) → RLS is a SQLite no-op anyway; append unarmed.
            self._ledger.append(batch)
            return
        with worker_workspace_scope(_uuid.UUID(self._workspace_id)):
            self._ledger.append(batch)

    async def _maybe_checkpoint(self) -> None:
        """Step 9: periodic 30 s checkpoint via the fenced conditional write (§8.4)."""
        assert self._shard is not None and self._checkpoints is not None
        if (time.monotonic() - self._last_checkpoint_at) < CHECKPOINT_INTERVAL_S:
            return
        await self._checkpoint()

    async def _checkpoint(self) -> None:
        assert self._shard is not None and self._checkpoints is not None
        self._checkpoint_seq += 1
        await self._checkpoints.save(
            self._shard,
            fencing_token=self._fencing_token,
            checkpoint_seq=self._checkpoint_seq,
            config_sha256=self._config_sha256,
        )
        self._last_checkpoint_at = time.monotonic()

    # -- reconcile branches ------------------------------------------------------

    async def _finalize(self) -> None:
        """T10 stop finalize: checkpoint (retained for T12), then release the lease.

        The checkpoint is retained for restart-as-continuation (T12; the seed is
        never re-rolled, INV-STR-5). On-stop late-buffer flush is a Phase 9 slot.
        Then the runner-converged lifecycle is written ``stopped`` and the lease is
        released so the shard is no longer claimable.  # Phase 9 (late-buffer flush)
        """
        from streams.domain.models import REASON_USER

        assert self._checkpoints is not None and self._shard is not None
        try:
            await self._checkpoint()
        except FencingError:
            # A takeover already happened; our finalize checkpoint is fenced. Leave
            # the durable state to the new holder and stop (zero post-takeover writes).
            logger.warning("shard.finalize_fenced", stream_id=self._stream_id)
            return
        await lifecycle.report_lifecycle(
            self._stream_id,
            lifecycle.STOPPED,
            status_reason=REASON_USER,
            workspace_id=self._workspace_id,
        )
        logger.info(
            "shard.stopped",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            emitted_total=self.emitted_total,
        )

    async def _enter_paused(self) -> None:
        """T5→T6 pause: halt within one tick, checkpoint synchronously, hold the lease.

        On the FIRST tick that observes desired ``paused`` (``_paused`` not yet set)
        the worker:

        * emits NOTHING this tick (the branch returns before generate/publish) — so
          emission halts within one tick, with no ``emitted_at`` later than the
          pause-convergence tick + one tick (exit criterion 1);
        * persists a checkpoint SYNCHRONOUSLY (the fenced conditional write) BEFORE
          reporting ``paused`` (T6 / §8.4 "once, synchronously before reporting
          paused") — the virtual clock is implicitly frozen at the frontier ``F``
          because no further segment advance happens while idling;
        * reports the runner-converged lifecycle ``paused`` (preserving the
          control-plane ``status_reason`` so ``paused_quota``/``paused_idle`` render).

        On every SUBSEQUENT paused tick the worker simply idles (lease retained by the
        supervisor's heartbeat; nothing emitted, no re-checkpoint). The warm engine
        state stays in memory for a zero-restore resume (T8).
        """
        if self._paused:
            return  # already converged to paused — idle this tick (lease retained)
        assert self._shard is not None
        # Synchronous checkpoint BEFORE reporting paused (T6). A stale token raises
        # FencingError (a takeover happened); the worker stops and the new holder owns
        # the durable state. Pause halts emission this tick: no generate/publish ran.
        await self._checkpoint()
        self._paused = True
        await self._report_paused()
        logger.info(
            "shard.paused",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            checkpoint_seq=self._checkpoint_seq,
            frontier_us=self._shard.clock.frontier_us,
        )

    async def _report_paused(self) -> None:
        """Converge lifecycle to ``paused``, preserving the desired ``status_reason``.

        The control plane wrote the pause reason (user/quota/idle); the runner
        converges the lifecycle column to ``paused`` while keeping that reason so
        ``Stream.status`` renders ``paused_quota``/``paused_idle`` (domain-model §4.3
        surfaced-status string). Re-reads the reason from the desired-state row so a
        system pause that changed it after the worker entered the branch is honoured.
        """
        from streams.domain.models import LC_PAUSED, REASON_USER

        desired = await asyncio.to_thread(desired_state.desired_for, self._stream_id)
        reason = desired.status_reason if desired is not None else REASON_USER
        await lifecycle.report_lifecycle(
            self._stream_id,
            LC_PAUSED,
            status_reason=reason,
            workspace_id=self._workspace_id,
        )

    async def _resume_from_paused(self) -> None:
        """T7→T8 resume: re-anchor the clock (dwell rebase) and report running again.

        The pause held the warm engine state in memory and froze the virtual clock at
        the frontier ``F``. Resume opens a fresh run segment anchored at
        ``(wall_now, F)`` so ``virtual_now`` continues from ``F`` and dwell timers —
        which store absolute virtual due-times — are rebased to the resumed virtual
        clock with no per-timer recomputation (§9.3 step 4). In-flight funnels continue
        with ZERO ``sequence_no`` gaps: the gapless counter, the heap, and the pools
        never left memory. The token bucket is re-primed to ``wall_now`` so the freeze
        interval does not credit a burst of stale tokens. Then lifecycle converges back
        to ``running`` (T8) and this same tick proceeds to generate.
        """
        assert self._shard is not None and self._bucket is not None
        now = self._wall.now()
        self._shard.reopen_clock_segment(now)
        # Re-anchor the bucket at the resumed wall instant: the paused interval must
        # not accrue tokens (pacing is wall-domain; a long pause would otherwise grant
        # a full-capacity burst on resume). A fresh bucket at the held rate re-anchors
        # ``_last`` to ``now`` cleanly (TokenBucket has no public reset).
        self._bucket = TokenBucket(rate_per_second=self._bucket.rate, now=now)
        self._paused = False
        await lifecycle.report_lifecycle(
            self._stream_id, lifecycle.RUNNING, workspace_id=self._workspace_id
        )
        logger.info(
            "shard.resumed",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            frontier_us=self._shard.clock.frontier_us,
            last_sequence_no=self._shard.sequence.last,
        )

    # -- timing ------------------------------------------------------------------

    async def _sleep_to_next_tick(self) -> None:
        """Sleep the remainder of the 1,000 ms tick (best-effort fixed cadence)."""
        await asyncio.sleep(self._tick_s)
