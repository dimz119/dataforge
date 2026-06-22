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
from typing import TYPE_CHECKING, Any, cast

import structlog

from config.logging import bind_log_context, emit_tick_summary, unbind_log_context
from dataforge_engine.behavior import (
    Shard,
    ShardConfig,
    compile_manifest_cached,
)
from dataforge_engine.behavior.scheduler import TokenBucket
from generation.infra.clock import SystemWallClock
from observation.infra import metrics
from runner import lifecycle
from runner.checkpoint_store import CheckpointStore, RestoredCheckpoint
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
LATE_BUFFER_TAKE_LIMIT = 500  # §6.2 scheduler page (paced re-emission)


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


class _DriftMenuView:
    """The engine's ``registry_view`` port (§11, DR-1): subject → next-version menu.

    A thin ``menu_for`` adapter over the desired-state ``registry_view`` snapshot
    (``{subject: DriftMenuEntry}``). The entries already satisfy the engine's
    ``DriftMenu`` protocol (``from_version`` / ``to_version`` / ``added_fields``), so
    no per-entry conversion is needed — this only realises the ``menu_for`` lookup.
    Returns ``None`` for a subject with no registered next version (ineligible — drift
    can never invent a field, DR-3 / CH-V07).
    """

    __slots__ = ("_menu",)

    def __init__(self, menu: dict[str, Any]) -> None:
        self._menu = menu

    def menu_for(self, subject: str) -> Any | None:
        return self._menu.get(subject)


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
        # Phase 9 chaos: the pipeline + the two append-only ports (recorder /
        # late_buffer). Built in _setup once the workspace + speed are known.
        self._chaos: Any | None = None  # ChaosPipeline
        self._recorder: Any | None = None  # chaos InjectionRecorder
        self._late_buffer: Any | None = None  # chaos LateArrivalBuffer
        self._chaos_subseed: bytes = b""  # HMAC(stream_seed, "chaos") (§4.1)
        # The drift field menu (§11, DR-1): {subject: DriftMenuEntry} from the
        # desired-state ``registry_view``, refreshed each poll. Wrapped per tick in a
        # ``menu_for`` provider and handed to the engine as the ``registry_view`` port.
        self._registry_view: dict[str, Any] = {}
        # Schema evolution (schema-registry §10.1-10.2): the materialized PIN-R1/R2 map
        # (resolved once at first start, then carried in the checkpoint unchanged on
        # restart) and the highest applied upgrade target per subject (the cutover
        # workstream P10-05/06 updates this). Both persist in the checkpoint ``runtime``
        # side-car and feed the engine's per-event-type ``schema_versions`` override.
        self._schema_pins: dict[str, int] = {}  # materialized {subject: version}
        self._applied_upgrades: dict[str, int] = {}  # {subject: highest applied target}
        # The desired schedule/effective signature the live IR was last compiled for. A
        # change (a newly-armed/changed scheduled upgrade, or an applied one raising the
        # effective map) triggers a recompile + ``Shard.retarget_ir`` (§10.4 step 1-2).
        self._cutover_sig: str = ""
        self._speed_multiplier: float = 1.0
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
        # Bind tenant correlation so every downstream data-plane log line on this
        # worker carries workspace/stream/shard (observability §3.1 / foundation).
        bind_log_context(
            workspace_id=self._workspace_id,
            stream_id=self._stream_id,
            shard_id=self._shard_id,
        )
        await lifecycle.report_lifecycle(
            self._stream_id, lifecycle.RUNNING, workspace_id=self._workspace_id
        )
        # LV-3 lifecycle transition (always emitted) instead of a raw INFO; the
        # data-plane per-tick INFO is the LV-1 rolled-up summary (see _tick).
        logger.info(
            "shard.running",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            fencing_token=self._fencing_token,
        )
        metrics.runner_streams_running.inc()
        try:
            while True:
                stop = await self._timed_tick()
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
        finally:
            metrics.runner_streams_running.dec()
            unbind_log_context()

    async def _timed_tick(self) -> bool:
        """Run one tick under df_runner_tick_duration_seconds + overrun accounting.

        Records the wall time of the §8.3 reconciliation tick (the M-5 inner-loop
        histogram), flags df_runner_tick_overruns_total when a tick runs past its
        1,000 ms budget (the fixed-cadence violation alerts watch), and emits the
        LV-1 ``runner.tick.summary`` (≤ 1 INFO/stream/60 s — the only data-plane
        INFO line, observability §2.2).
        """
        started = time.monotonic()
        stop = await self._tick()
        elapsed = time.monotonic() - started
        metrics.runner_tick_duration_seconds.observe(elapsed)
        if elapsed > self._tick_s:
            metrics.runner_tick_overruns_total.inc()
        emit_tick_summary(
            logger,
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            ticks=self.ticks,
            emitted_total=self.emitted_total,
            tick_ms=round(elapsed * 1000, 1),
            paused=self._paused,
        )
        return stop

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
        # Schema evolution: the checkpoint store is needed before the IR compile so a
        # takeover can restore the materialized pin map (resolved once, §10.1) before
        # compiling the IR with the effective per-event-type schema_versions override.
        self._checkpoints = CheckpointStore(
            workspace_id=str(desired.workspace_id),
            stream_id=self._stream_id,
            shard_id=self._shard_id,
        )
        restored = await self._checkpoints.load()
        await self._init_schema_versions(desired, restored)
        ir = compile_manifest_cached(
            desired.pinned_config,
            config_sha256=self._config_sha256,
            schema_versions=self._engine_schema_versions(desired),
        )
        self._shard = self._build_shard(ir, desired)
        # The live IR reflects the base effective map with no armed cutover — record that
        # signature so the first tick only recompiles if a scheduled upgrade is armed
        # (§10.4: steady state with no pending upgrade recompiles nothing).
        self._cutover_sig = self._cutover_signature(desired, {})
        self._ledger = self._build_ledger(desired)
        self._bucket = TokenBucket(
            rate_per_second=desired.target_tps / self._shard_count,
            now=self._wall.now(),
        )
        self._speed_multiplier = float(desired.speed_multiplier)
        self._build_chaos(desired)
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
                schema_pins=self._schema_pins,
            )
        else:
            # First start (T1→T3): seed head snapshots, append them BEFORE chaos
            # (INV-GEN-5), publish them so consumers see the catalog, then persist the
            # FIRST checkpoint. The materialized pin (PIN-R1, resolved once above) rides
            # that checkpoint's ``runtime`` side-car so restarts/failover continue it
            # unchanged — "latest" is never re-resolved (§10.1). The checkpoint captures
            # the seeded engine state so a takeover within the 30 s window is correct.
            head = _deterministic_emitted_at(self._shard.seed())
            if head:
                await asyncio.to_thread(self._append_ledger, head)
                self._publisher.publish(head)
                await lifecycle.incr_emitted(self._redis, self._stream_id, len(head))
            await self._checkpoint()
        self._last_checkpoint_at = time.monotonic()

    async def _init_schema_versions(
        self, desired: DesiredState, restored: RestoredCheckpoint | None
    ) -> None:
        """Resolve the materialized pin map (PIN-R1/R2) — restore it, or materialize once.

        On takeover/restart the materialized map + applied-upgrade targets are restored
        from the checkpoint ``runtime`` side-car (resolved exactly once per stream,
        §10.1 — restarts never re-resolve "latest"). On first start (no checkpoint) the
        map is materialized now from the pinned manifest's emitted subjects + the
        explicit ``schema_version_pins`` overrides; the subsequent first-checkpoint
        write (in ``_setup``) persists it. The DB read runs off the event loop.
        """
        if restored is not None:
            runtime = restored.runtime or {}
            self._schema_pins = {
                str(k): int(v) for k, v in (runtime.get("schema_pins") or {}).items()
            }
            self._applied_upgrades = {
                str(k): int(v) for k, v in (runtime.get("applied_upgrades") or {}).items()
            }
            return
        from streams.application.schema_pins import materialize_pins

        self._schema_pins = await asyncio.to_thread(
            materialize_pins,
            dict(desired.schema_version_pins or {}),
            manifest=desired.pinned_config,
        )
        self._applied_upgrades = {}

    def _effective_versions(self) -> dict[str, int]:
        """The §10.2 effective ``{subject: version}`` = max(materialized pin, applied)."""
        from streams.application.schema_pins import effective_versions

        return effective_versions(self._schema_pins, self._applied_upgrades)

    def _engine_schema_versions(self, desired: DesiredState) -> dict[str, int]:
        """Re-key the effective map to the engine's ``{event_type: version}`` override."""
        from streams.application.schema_pins import engine_schema_versions

        return engine_schema_versions(
            self._effective_versions(), manifest=desired.pinned_config
        )

    def _runtime_state(self) -> dict[str, Any]:
        """The checkpoint ``runtime`` side-car: materialized pin + applied targets (§10.2)."""
        return {
            "schema_pins": dict(self._schema_pins),
            "applied_upgrades": dict(self._applied_upgrades),
        }

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

    def _build_chaos(self, desired: DesiredState) -> None:
        """Build the chaos pipeline + the recorder / durable-buffer ports (§6).

        The pipeline is pure (it never touches Django); the recorder
        (``chaos_injections``) and the late buffer (``late_arrival_buffer``) are the
        two Postgres-backed ports. The buffer is DURABLE state — a fresh instance
        under a new lease holder picks up pending entries via ``take_due`` on its
        first tick (§6.3 failover), which is why no in-memory hand-off is needed.
        """
        from chaos.application.services import resolve_policy
        from chaos.infra.late_buffer import LateArrivalBuffer
        from chaos.infra.recorder import InjectionRecorder
        from dataforge_engine.chaos import ChaosPipeline, chaos_subseed

        self._chaos = ChaosPipeline(resolve_policy(desired.chaos_config))
        self._chaos_subseed = chaos_subseed(int(desired.seed))
        # The drift field menu the engine draws from (§11, DR-1) — refreshed each poll.
        self._registry_view = dict(desired.registry_view)
        self._recorder = InjectionRecorder()
        self._late_buffer = LateArrivalBuffer(
            workspace_id=str(desired.workspace_id),
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            publish=self._publisher.publish,
            speed_multiplier=self._speed_multiplier,
        )
        self._chaos_config_sha = _config_sha256(desired.chaos_config or {})

    def _apply_chaos_config(self, desired: DesiredState) -> None:
        """Rebuild the pipeline if the live ``chaos_config`` changed (§3.5).

        Toggles/rates/params apply at the next tick boundary. The recorder and the
        durable buffer are unaffected — disabling a mode never un-records truth nor
        drops pending entries (§3.5 mid-flight rules). Disabling ``late_arriving``
        stops new selections only; pending entries still emit at ``due_at``.
        """
        if self._chaos is None:
            return
        from chaos.application.services import resolve_policy
        from dataforge_engine.chaos import ChaosPipeline

        # Refresh the drift menu every poll (DR-1) so a mid-stream upgrade rebuilds it
        # automatically (DR-4): the desired-state read recomputes it against the
        # current effective version. Cheap dict copy; independent of the chaos sha.
        self._registry_view = dict(desired.registry_view)
        sha = _config_sha256(desired.chaos_config or {})
        if sha == self._chaos_config_sha:
            return
        self._chaos = ChaosPipeline(resolve_policy(desired.chaos_config))
        self._chaos_config_sha = sha

    # -- schema cutover (schema-registry §10.4) ----------------------------------

    async def _apply_schema_cutovers(self, desired: DesiredState) -> None:
        """Pre-warm + arm scheduled upgrades, then record any the clock has crossed.

        Runs each running tick AFTER the desired poll and BEFORE generate (§10.4):

        1. **Pre-warm** every ``scheduled`` upgrade into an engine
           :class:`~dataforge_engine.behavior.ir.SchemaCutover` from the desired
           ``schema_upgrade_schedule`` + the effective map (the ``registry_view``
           pre-warm, EM-5; one registry read off the event loop). When the resulting
           overlay differs from the live IR's (a new/changed schedule, or an applied
           upgrade raising the effective version), recompile the IR with the cutover
           ``schema_versions`` + ``schema_cutovers`` and ``retarget_ir`` the live shard —
           pools/heap/sequence/clock are shared by reference, so generation continues
           with zero ``sequence_no`` gaps (the "no restart" cutover).
        2. **Apply bookkeeping** for any pre-warmed cutover whose ``at`` the virtual
           clock has now reached (``virtual_now ≥ at``): fold the target into the
           effective map (``_applied_upgrades``), capture the first post-cutover
           ``sequence_no`` (the next event the shard emits, ``sequence.last + 1``), and
           write the ``applied`` transition + ``applied_at_wall`` back to the schedule
           entry (audit ``schema_upgrade_applied``). The actual per-event version switch
           is the interpreter's ``occurred_at`` gate, not this step — this only records
           that the boundary has passed and rebuilds the drift menu next refresh (DR-4).
        """
        assert self._shard is not None
        if self._shard.clock.is_backfill:
            # Backfill cutover is handled inside the batch run (the clock has no live
            # ``virtual_now``); the §10.4 backfill row is covered by the per-event gate
            # over the simulated window. A live tick never runs in backfill mode.
            return
        now = self._wall.now()
        virtual_now_us = self._shard.clock.virtual_now_us(now)
        cutovers = await asyncio.to_thread(
            self._pre_warm_cutovers, desired, virtual_now_us
        )
        self._maybe_retarget(desired, cutovers)
        await self._record_crossed_cutovers(desired, cutovers, virtual_now_us, now)

    def _pre_warm_cutovers(
        self, desired: DesiredState, virtual_now_us: int
    ) -> dict[str, Any]:
        """Build the ``{event_type: SchemaCutover}`` overlay for the armed schedule (EM-5)."""
        assert self._shard is not None
        from streams.application.schema_cutover import pre_warm_cutovers

        return pre_warm_cutovers(
            manifest=desired.pinned_config,
            effective=self._effective_versions(),
            schedule=desired.schema_upgrade_schedule,
            virtual_epoch_ms=self._shard.clock.virtual_epoch_ms,
            virtual_now_offset_us=virtual_now_us,
        )

    def _maybe_retarget(
        self, desired: DesiredState, cutovers: dict[str, Any]
    ) -> None:
        """Recompile + ``retarget_ir`` when the schema overlay changed (§10.4 step 1-2)."""
        assert self._shard is not None
        signature = self._cutover_signature(desired, cutovers)
        if signature == self._cutover_sig:
            return
        ir = compile_manifest_cached(
            desired.pinned_config,
            config_sha256=self._config_sha256,
            schema_versions=self._engine_schema_versions(desired),
            schema_cutovers=cutovers or None,
        )
        self._shard.retarget_ir(ir)
        self._cutover_sig = signature
        logger.info(
            "shard.schema_retargeted",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            effective=self._effective_versions(),
            armed=sorted(cutovers),
        )

    def _cutover_signature(
        self, desired: DesiredState, cutovers: dict[str, Any]
    ) -> str:
        """A stable signature of the schema overlay the IR must reflect.

        Folds the effective per-event-type map (so an applied upgrade forces a recompile
        that bakes the new base version) with each armed cutover's gate + target + added
        field names (the binding closures are not hashable, but the field names + target
        + ``at`` fully discriminate a schedule change). Order-independent via sorting.
        """
        effective = sorted(self._engine_schema_versions(desired).items())
        armed = sorted(
            (
                event_type,
                cut.at_us,
                cut.target_version,
                tuple(name for name, _ in cut.added_bindings),
            )
            for event_type, cut in cutovers.items()
        )
        return repr((effective, armed))

    async def _record_crossed_cutovers(
        self,
        desired: DesiredState,
        cutovers: dict[str, Any],
        virtual_now_us: int,
        wall_now: Any,
    ) -> None:
        """Mark applied every armed cutover the virtual clock has crossed (§10.4 step 4)."""
        assert self._shard is not None
        event_to_subject = {
            v: k for k, v in self._subject_event_types(desired).items()
        }
        for event_type, cut in cutovers.items():
            if virtual_now_us < cut.at_us:
                continue  # not yet — keep emitting the old version (cutover rule)
            subject = event_to_subject.get(event_type)
            if subject is None:
                continue
            if self._applied_upgrades.get(subject, 0) >= cut.target_version:
                continue  # already folded into the effective map (restart/failover)
            # The first post-cutover sequence_no per shard (§10.4 step 4): the next
            # event the shard will emit this tick is the first whose occurred_at ≥ at.
            applied_sequence_no = self._shard.sequence.last + 1
            self._applied_upgrades[subject] = cut.target_version
            await self._mark_applied(
                desired=desired,
                cutover=cut,
                subject=subject,
                applied_sequence_no=applied_sequence_no,
                wall_now=wall_now,
            )
            # Refresh the signature so the recompile this same tick bakes the now-applied
            # version as the base (the cutover entry drops out next pre-warm).
            self._cutover_sig = ""

    async def _mark_applied(
        self,
        *,
        desired: DesiredState,
        cutover: Any,
        subject: str,
        applied_sequence_no: int,
        wall_now: Any,
    ) -> None:
        """Persist the ``applied`` schedule transition + heartbeat (§10.4 step 4)."""
        upgrade_id = self._scheduled_upgrade_id(desired, subject)
        if upgrade_id is None:
            return
        await asyncio.to_thread(
            self._mark_applied_sync,
            upgrade_id=upgrade_id,
            applied_sequence_no=applied_sequence_no,
            wall_now=wall_now,
        )
        logger.info(
            "shard.schema_upgrade_applied",
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            subject=subject,
            target_version=cutover.target_version,
            applied_sequence_no=applied_sequence_no,
        )

    def _mark_applied_sync(
        self, *, upgrade_id: str, applied_sequence_no: int, wall_now: Any
    ) -> None:
        from streams.application.schema_upgrades import mark_upgrade_applied

        with self._armed_scope():
            mark_upgrade_applied(
                stream_id=self._stream_id,
                upgrade_id=upgrade_id,
                applied_at_wall=wall_now,
                applied_sequence_no=applied_sequence_no,
            )

    def _scheduled_upgrade_id(
        self, desired: DesiredState, subject: str
    ) -> str | None:
        """The ``upgrade_id`` of the subject's single ``scheduled`` entry (REG-U007)."""
        for entry in desired.schema_upgrade_schedule or []:
            if (
                isinstance(entry, dict)
                and entry.get("status") == "scheduled"
                and str(entry.get("subject")) == subject
            ):
                return str(entry.get("upgrade_id"))
        return None

    def _subject_event_types(self, desired: DesiredState) -> dict[str, str]:
        from streams.application.schema_pins import subject_to_event_type

        return subject_to_event_type(desired.pinned_config)

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
        # Live chaos config: rebuild the pipeline from the polled desired-state
        # document so toggles/rates apply at the next tick boundary (§3.5, ≤ 2 s).
        self._apply_chaos_config(desired)

        # 3.5 schema cutover (schema-registry §10.4): AFTER the desired poll, BEFORE
        #     generate — pre-warm a scheduled upgrade into the engine IR (the per-event
        #     ``occurred_at`` gate does the atomic-between-events switch), then record
        #     ``applied`` bookkeeping for any cutover the virtual clock has now crossed.
        await self._apply_schema_cutovers(desired)

        # 4. generate (paced).
        out = await self._generate_tick()

        # 5-7. ledger BEFORE chaos, chaos transform + late buffer, publish.
        published = await self._emit(out)

        # 8. stats.incr (Redis counters, INV-OBS-2) + events/day metering (P11-07).
        await lifecycle.incr_emitted(self._redis, self._stream_id, published)
        self.emitted_total += published
        if published > 0 and self._workspace_id:
            day_total = await lifecycle.incr_workspace_events_today(
                self._redis, self._workspace_id, published
            )
            await self._maybe_quota_pause(day_total)

        # 9. checkpoint every 30 s (fenced; raises FencingError on a stale token).
        await self._maybe_checkpoint()
        return False

    async def _maybe_quota_pause(self, day_total: int) -> None:
        """System-pause this stream if its workspace crossed the events/day cap (P11-07).

        The events/day exhaustion TRIGGER (PRD §7): once the workspace's UTC-day
        total reaches its ``events_per_day`` cap, pause to ``paused_quota`` (NEVER a
        delete — INV-TEN-5) so emission halts within the tick. The control-plane
        write (desired ``paused`` + audit + ``df_quota_pauses_total``) runs in a
        worker thread under the workspace-armed RLS scope (``system_pause`` opens its
        own transaction). The cap read is best-effort: ``day_total == 0`` means the
        Redis meter is degraded → fail-open (no pause). Idempotent: a stream already
        paused-desired is a ``system_pause`` no-op, so the per-tick check is cheap.
        """
        if day_total <= 0:
            return  # degraded meter (fail-open) — never pause on a counter miss
        await asyncio.to_thread(self._quota_pause_if_over, day_total)

    def _quota_pause_if_over(self, day_total: int) -> None:
        """Blocking half of :meth:`_maybe_quota_pause` (runs in a worker thread)."""
        import uuid as _uuid

        from streams.application import quotas, services
        from streams.domain.models import Stream
        from tenancy.application.services import worker_workspace_scope

        ws = _uuid.UUID(self._workspace_id)
        cap = quotas.events_per_day_cap(ws)
        if cap <= 0 or day_total < cap:
            return
        with worker_workspace_scope(ws):
            stream = Stream.objects.filter(id=self._stream_id).first()
            if stream is None:
                return
            services.system_pause(stream=stream, reason="quota")

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
        """Steps 5-7: ledger append (BEFORE chaos), chaos transform, publish + due.

        INV-GEN-5: the ledger sees the clean batch before any downstream stage.
        Then the chaos pipeline transforms delivery truth (§2.2): records each
        injection BEFORE the affected instance is published/extracted (INV-CHA-4),
        extracts ``late_arriving`` selections into the durable buffer, and the
        scheduler re-emits any entries now due (§6.2). The due poll runs every
        running tick — even on a quiet tick — so a post-resume overdue backlog
        drains promptly (§6.3 resume row).
        """
        assert self._shard is not None and self._ledger is not None
        published = 0
        if batch:
            # 5. durable BEFORE chaos reads it (INV-GEN-5).
            await asyncio.to_thread(self._append_ledger, batch)
            # 6. chaos.transform — content + temporal stages; late selections leave
            #    the in-line flow into the durable buffer (recorded first, INV-CHA-4).
            out = await asyncio.to_thread(self._run_chaos, batch)
            # 7. publish the surviving/added in-line instances, keyed by partition_key.
            published += self._publisher.publish(out)
            # df_generation_events_total{event_class}: count each published event by
            # its CDC op (c/u/d/r) — a bounded label (4 values), M-3-safe (§4 runner).
            self._count_generation(out)
        # 5b. the late-arrival scheduler: publish entries now due (§6.2). Re-emissions
        #     go through the publisher inside take_due; count them into the total.
        published += await self._take_due_late()
        return published

    def _run_chaos(self, batch: list[Any]) -> list[Any]:
        """Run the pure pipeline, then persist its side effects (recorder + buffer).

        Records flush BEFORE the late entries persist and BEFORE publish (INV-CHA-4);
        the late buffer's ``schedule`` then durably stores the tick's selections. All
        on the worker thread so the RLS GUC lives on the ORM's connection.
        """
        if self._chaos is None:  # unit-test fallback (no _setup): identity.
            return batch
        assert self._recorder is not None and self._late_buffer is not None
        ctx = self._chaos_context()
        with self._armed_scope():
            out: list[Any] = self._chaos.transform(batch, ctx)
            self._recorder.flush()  # answer-key rows BEFORE publish/extraction
            self._late_buffer.schedule()  # durable pending entries (INV-CHA-5)
        return out

    async def _take_due_late(self) -> int:
        """Step 5b: re-emit due late-buffer entries this tick (§6.2 scheduler)."""
        if self._late_buffer is None:
            return 0
        return await asyncio.to_thread(self._drain_due)

    def _drain_due(self) -> int:
        assert self._late_buffer is not None
        with self._armed_scope():
            return int(
                self._late_buffer.take_due(self._wall.now(), limit=LATE_BUFFER_TAKE_LIMIT)
            )

    def _chaos_context(self) -> Any:
        """A per-tick :class:`StageContext` bound to the recorder + buffer ports."""
        from dataforge_engine.chaos import StageContext

        assert self._shard is not None
        clock = type("Clk", (), {"speed_multiplier": self._speed_multiplier})()
        return StageContext(
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            workspace_id=self._workspace_id,
            chaos_subseed=self._chaos_subseed,
            recorder=cast(Any, self._recorder),
            late_buffer=cast(Any, self._late_buffer),
            registry_view=_DriftMenuView(self._registry_view),
            virtual_clock=clock,
        )

    def _armed_scope(self) -> Any:
        """Workspace-armed transaction scope for the chaos Postgres writes (§4.2)."""
        import contextlib
        import uuid as _uuid

        from tenancy.application.services import worker_workspace_scope

        if not self._workspace_id:
            return contextlib.nullcontext()
        return worker_workspace_scope(_uuid.UUID(self._workspace_id))

    @staticmethod
    def _count_generation(batch: list[Any]) -> None:
        """df_generation_events_total{event_class} per emitted event (op c/u/d/r).

        ``op`` is the CDC operation class — a closed 4-value set, an admissible
        bounded label (M-3 bans only workspace/stream/user/event ids). An envelope
        without an ``op`` falls back to ``unknown`` (defensive; the engine always
        stamps one).
        """
        for env in batch:
            op = env.get("op") if hasattr(env, "get") else None
            metrics.generation_events_total.labels(
                event_class=str(op) if op else "unknown"
            ).inc()

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
        # df_ledger_append_duration_seconds (M-5 inner-loop) + df_ledger_append_
        # failures_total (§4 runner family). The append is the canonical-truth write
        # (INV-GEN-5); a failure here is release-critical (the alert source).
        started = time.monotonic()
        try:
            if not self._workspace_id:
                # No setup (unit test) → RLS is a SQLite no-op anyway; append unarmed.
                self._ledger.append(batch)
            else:
                with worker_workspace_scope(_uuid.UUID(self._workspace_id)):
                    self._ledger.append(batch)
        except Exception:
            metrics.ledger_append_failures_total.inc()
            raise
        finally:
            metrics.ledger_append_duration_seconds.observe(time.monotonic() - started)

    async def _maybe_checkpoint(self) -> None:
        """Step 9: periodic 30 s checkpoint via the fenced conditional write (§8.4)."""
        assert self._shard is not None and self._checkpoints is not None
        if (time.monotonic() - self._last_checkpoint_at) < CHECKPOINT_INTERVAL_S:
            return
        await self._checkpoint()

    async def _checkpoint(self) -> None:
        assert self._shard is not None and self._checkpoints is not None
        self._checkpoint_seq += 1
        # df_checkpoint_duration_seconds (M-5 inner-loop): the fenced conditional
        # write latency (§8.4). df_checkpoint_age_seconds is reset to 0 on a fresh
        # commit; the supervisor/alert reads its growth (CheckpointStale ticket).
        started = time.monotonic()
        await self._checkpoints.save(
            self._shard,
            fencing_token=self._fencing_token,
            checkpoint_seq=self._checkpoint_seq,
            config_sha256=self._config_sha256,
            runtime=self._runtime_state(),
        )
        now = time.monotonic()
        metrics.checkpoint_duration_seconds.observe(now - started)
        metrics.checkpoint_age_seconds.set(0)
        self._last_checkpoint_at = now

    # -- reconcile branches ------------------------------------------------------

    async def _finalize(self) -> None:
        """T10 stop finalize: apply OnStopPolicy, checkpoint, then release the lease.

        On-stop the stream's ``on_stop_policy`` (§6.3) governs pending late
        re-emissions — ``discard`` (default) marks them ``discarded``; ``flush``
        publishes every pending entry now (ignoring ``due_at``) before the lease is
        released, marking them ``emitted`` with injection ``outcome: flushed``. The
        checkpoint is retained for restart-as-continuation (T12; the seed is never
        re-rolled, INV-STR-5). Then the runner-converged lifecycle is written
        ``stopped`` and the lease released so the shard is no longer claimable.
        """
        from streams.domain.models import REASON_USER

        assert self._checkpoints is not None and self._shard is not None
        await self._apply_on_stop_policy()
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

    async def _apply_on_stop_policy(self) -> None:
        """Resolve + apply the stream's ``on_stop_policy`` to pending late entries (§6.3).

        Re-reads the desired-state document so the policy value in effect at the
        moment the stop is processed applies (§3.2). ``flush`` re-emissions count
        into ``emitted_total``; ``discard`` (default) records ``outcome: discarded``.
        """
        if self._late_buffer is None:
            return
        from chaos.application.services import resolve_on_stop_policy

        desired = await asyncio.to_thread(desired_state.desired_for, self._stream_id)
        config = desired.chaos_config if desired is not None else None
        policy = resolve_on_stop_policy(config)
        published = await asyncio.to_thread(self._run_on_stop, policy)
        if published:
            await lifecycle.incr_emitted(self._redis, self._stream_id, published)
            self.emitted_total += published

    def _run_on_stop(self, policy: str) -> int:
        assert self._late_buffer is not None
        now = self._wall.now()
        with self._armed_scope():
            if policy == "flush":
                return int(self._late_buffer.flush_pending(now))
            self._late_buffer.discard_pending(now)
            return 0

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
