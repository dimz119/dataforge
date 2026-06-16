"""Shard — the engine entrypoint (behavior-engine §3.3, §3.5, §8).

A :class:`Shard` owns one shard's generation state: pools, virtual clock, timer
heap, arrival process, interpreter, and the gapless sequence counter. It exposes
:meth:`generate` (the §3.3 ``generate(budget, until)`` call inside the runner
tick) and :meth:`run_batch` (the §8 backfill / batch-finalization core — unpaced,
bounded by max-events or window-end). Actor binding follows BE-A1 (uniform draw +
circular scan over the eligible actor registry).

The Shard is host-agnostic: wall time and the ledger are injected
(:class:`~dataforge_engine.ports.WallClock`, :class:`~dataforge_engine.ports.LedgerSink`),
and golden/dry-run/runner hosts all drive the same code. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dataforge_engine.envelope import event_id_for
from dataforge_engine.seeds import SeedTree

from .background import US_PER_DAY, BackgroundMutationDriver
from .clock import VirtualClock
from .interpreter import Interpreter
from .pools import EntityPools
from .rng import Cursor, UuidBits, traversal_rng
from .runtime import Traversal
from .scheduler import ArrivalProcess, Timer, TimerHeap
from .seeding import seed_pools
from .transaction import SequenceCounter, StreamIdentity

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.ports import LedgerSink, WallClock

    from .ir import ManifestIR

# Worst-case events per transaction: 1 business + 8 CDC (B-07). The generate loop
# checks headroom BEFORE interpreting so a transaction is never split (§3.3).
MAX_EVENTS_PER_TX = 9


@dataclass
class ShardConfig:
    """The pinned per-stream/shard inputs the Shard needs (behavior-engine §11)."""

    seed: int
    workspace_id: str
    stream_id: str
    shard_id: int
    virtual_epoch: datetime
    speed_multiplier: float = 1.0
    shard_count: int = 1
    mode: str = "live"  # "live" | "backfill"
    mean_events_per_session: float = 1.0
    visits_per_actor_day: float = 1.0
    # The flat TPS the engine core uses for live arrivals; the runner supplies the
    # recorded TPS schedule (§3.6) in Phase 5. 0.0 keeps a live shard idle until a
    # host wires the schedule.
    target_tps: float = 0.0
    catalog_overrides: dict[str, int] | None = None
    schema_versions: dict[str, int] | None = None


class Shard:
    """One shard's generation engine — the run/batch core."""

    def __init__(self, ir: ManifestIR, config: ShardConfig, clock_port: WallClock) -> None:
        self._ir = ir
        self._config = config
        self._clock_port = clock_port
        self._tree = SeedTree(config.seed)
        self._pools = EntityPools(ir.actor_entity)
        self._vclock = VirtualClock(
            virtual_epoch=config.virtual_epoch,
            speed_multiplier=config.speed_multiplier,
            mode=config.mode,
        )
        self._heap = TimerHeap()
        self._sequence = SequenceCounter()
        self._identity = StreamIdentity(
            workspace_id=config.workspace_id, stream_id=config.stream_id,
            shard_id=config.shard_id, scenario_slug=ir.slug,
            manifest_version=ir.version,
        )
        self._interp = Interpreter(
            ir, self._pools, self._heap, self._identity, self._sequence,
            self._vclock, self._tree,
        )
        self._arrival = ArrivalProcess(
            Cursor(self._tree.key("transitions", f"arrival:{config.shard_id}"))
        )
        # Background mutations (R-CDC-3): CDC-only chain-root drift, swept once per
        # simulated day. The driver is inert when no entity declares rules.
        self._bg = BackgroundMutationDriver(
            ir, self._pools, self._vclock, self._tree, self._identity, self._sequence,
        )
        self._seeded = False
        # Optional simulated-µs horizon after which no *new* arrival is scheduled —
        # the in-flight lifecycles still drain to ``until_us`` (BE-A: arrivals are a
        # self-perpetuating chain, so capping the chain bounds the session population
        # while every spawned lifecycle completes). ``None`` = arrivals run for the
        # whole window (the default; backfill density is otherwise BE-F4-fixed).
        self._arrival_until_us: int | None = None

    # -- properties for hosts ----------------------------------------------

    @property
    def sequence(self) -> SequenceCounter:
        return self._sequence

    @property
    def pools(self) -> EntityPools:
        return self._pools

    @property
    def clock(self) -> VirtualClock:
        return self._vclock

    @property
    def heap(self) -> TimerHeap:
        return self._heap

    @property
    def interpreter(self) -> Interpreter:
        return self._interp

    @property
    def tree(self) -> SeedTree:
        return self._tree

    @property
    def config(self) -> ShardConfig:
        return self._config

    @property
    def ir(self) -> ManifestIR:
        return self._ir

    @property
    def arrival(self) -> ArrivalProcess:
        return self._arrival

    def set_arrival_state(self, state: object) -> None:
        """Replace the arrival integrator position (checkpoint restore, §9.3).

        Rebases the arrival gap-draw cursor to the restored ``next_index`` — the
        cursor position is a derivable invariant (one draw per arrival), so a fresh
        restored shard must re-anchor it or the next inter-arrival gap diverges
        (GOLD-D continuation correctness)."""
        from .scheduler import ArrivalState
        if isinstance(state, ArrivalState):
            self._arrival.state = state
            self._arrival.rebase_cursor()

    def ensure_registered(self) -> None:
        """Register all entity types + relationships from the IR (idempotent).

        Seeding does this; restore calls it so per-type counters and indexes exist
        even before the host loads pool images (§9.3 step 2).
        """
        for name in self._ir.entity_order:
            self._pools.register_type(name)
        for rel_name, src, attr, tgt in self._ir.relationships:
            self._pools.register_relationship(rel_name, src, attr, tgt)

    def mark_seeded(self) -> None:
        """Mark the shard already seeded (restore path: do not re-seed, §9.3)."""
        self._seeded = True
        self._vclock.open_segment(self._clock_port.now())

    def restore_sequence(self, last: int) -> None:
        """Resume the gapless ``sequence_no`` counter (checkpoint restore)."""
        self._sequence.reset_to(last)

    def set_target_tps(self, target_tps: float) -> None:
        """Adopt a new live ``target_tps`` (the §4.4 live-mutable slot; BE-P2).

        The runner calls this each tick from the polled desired state. ``target_tps``
        feeds ``_rho`` (sessions per simulated second, §3.5), so the next scheduled
        arrival integrates at the new density — the engine-side half of a TPS change;
        the runner's token bucket adopts the matching wall-rate the same tick. Content
        stays TPS-independent (BE-P4): per-session draws are keyed by traversal
        identity, the rate only changes *which* arrivals occur and *when* in simulated
        time. The full recorded-schedule step function with `effective_virtual_at`
        anchors (BE-P1/P3 — intensity curves, multi-entry pruning) lands in Phase 8;
        the live-mutable slot and the determinism input exist now.
        """
        self._config.target_tps = float(target_tps)

    def reopen_clock_segment(self, wall_now: datetime) -> None:
        """Re-anchor the virtual clock at ``(wall_now, frontier_us)`` (resume, §9.3 step 4).

        The pause froze the clock at the generation frontier ``F`` (no segment
        advances while idling). On resume the runner opens a fresh segment anchored to
        the resumed wall instant, so ``virtual_now`` continues from ``F`` — and dwell
        timers, which store *absolute* virtual due-times, are thereby rebased to the
        frozen-then-resumed virtual clock with no per-timer recomputation. In-flight
        funnels continue with zero ``sequence_no`` gaps (T8): nothing about a traversal
        lived outside the warm in-memory state held across the pause.
        """
        self._vclock.open_segment(wall_now)

    # -- seeding (§4.5) -----------------------------------------------------

    def seed(self) -> list[InternalEnvelope]:
        """Seed pools + return the head-of-stream ``op:"r"`` snapshot batch."""
        if self._seeded:
            return []
        self._seeded = True
        emitted_at = self._clock_port.now()
        self._vclock.open_segment(emitted_at)
        snapshots = seed_pools(
            self._ir, self._pools, self._tree, self._vclock, self._identity,
            self._sequence, emitted_at=emitted_at,
            overrides=self._config.catalog_overrides,
        )
        self._schedule_first_arrival()
        self._schedule_background_day(0)
        return snapshots

    # -- background mutations (R-CDC-3) ------------------------------------

    def _schedule_background_day(self, day_index: int) -> None:
        """Push the ``background_day`` planning timer for simulated day ``day_index``.

        The planner runs at the day's *start* ``v = day_index . US_PER_DAY`` so the
        per-instance within-day offsets fall inside the same day. It carries the
        index so the next day re-arms when processed (self-perpetuating, like
        arrivals). Inert when no rule is declared (no timer is ever pushed).
        """
        if not self._bg.has_rules:
            return
        due = day_index * US_PER_DAY
        self._heap.push(due, "background_day", {"day_index": day_index})

    def _handle_background_day(self, timer: Timer) -> list[InternalEnvelope]:
        """Plan one simulated day: push a ``bg_mutation`` timer per firing.

        Each firing emits exactly one CDC event at fire time (≤ 1 envelope per heap
        pop, so the generate-loop headroom check is never violated). The next day's
        planner is armed unconditionally — the heap window/budget bounds the cadence.
        """
        day_index = int(timer.ref.get("day_index", 0))
        for due, ref in self._bg.plan_day(day_index):
            self._heap.push(due, "bg_mutation", ref)
        self._schedule_background_day(day_index + 1)
        return []

    def _handle_bg_mutation(self, timer: Timer, emitted_at: datetime) -> list[InternalEnvelope]:
        return self._bg.fire(timer.ref, emitted_at=emitted_at)

    # -- arrivals (§3.5, BE-A1) --------------------------------------------

    def _backfill_catalog_size(self) -> int:
        """The actor catalog size that drives backfill arrival density (BE-F4).

        BE-F4 fixes backfill ``rho`` *for the whole dataset*: it is the **seeded**
        actor catalog size — the manifest ``seeding.catalogs`` default for the actor
        entity, overridable per instance within the declared min/max — not the live
        pool count. Using the live count lets ``user_registered`` creates inflate
        ``rho`` over the window, producing a secular session-volume ramp that bends the
        diurnal/weekly shape (STAT-SHAPE-2) and overshoots the PRD E7 / admission-estimate
        envelope (``simulated_days x catalog_size x visits x mes``, behavior-engine §3.5).
        Derived purely from manifest + config, so it is restore-stable by construction.

        This is a **Phase-8 backfill-arrival semantic**, gated on
        ``phase8_features`` alongside the intensity curves and background mutations it
        composes with (§3.4/§3.5): pre-1.1.0 published manifests keep the live-count
        behavior they were golden-baselined under (GOLD-A byte-stability, P-6/INV-GEN-3),
        while the 1.1.0 full manifest gets the spec-correct fixed density.
        """
        actor = self._ir.actor_entity
        overrides = self._config.catalog_overrides or {}
        return int(overrides.get(actor, self._ir.seeding.get(actor, 0)))

    def _rho(self) -> float:
        """Base arrival density rho (sessions per simulated second)."""
        if self._vclock.is_backfill:
            catalog = (
                self._backfill_catalog_size()
                if self._ir.phase8_features
                else self._pools.count(self._ir.actor_entity)
            )
            return catalog * self._config.visits_per_actor_day / 86_400.0
        mes = self._config.mean_events_per_session
        tps = self._current_tps()
        denom = mes * self._config.shard_count * self._config.speed_multiplier
        return tps / denom if denom > 0 else 0.0

    def _current_tps(self) -> float:
        # The recorded TPS schedule is a runner concern (Phase 5); the engine core
        # uses a flat target carried in config for batch/golden (§3.6).
        return self._config.target_tps

    def _next_arrival_us(self) -> int | None:
        """Solve the next arrival µs under ``λ(v) = rho x intensity(v)`` (§3.4/§3.5).

        Routes through the renormalized diurnal x weekly curve from the IR. A flat
        curve (no manifest `intensity`) reduces to the constant-rate solve, so
        ``target_tps`` stays the exact daily average; a declared curve reshapes
        *when* arrivals land without moving the daily mean (mean-1.0 renorm)."""
        return self._arrival.next_arrival_us_curved(
            self._rho(), self._ir.intensity, self._vclock.virtual_epoch_ms
        )

    def _schedule_arrival(self, due: int | None) -> None:
        """Push the next arrival timer unless it lands past the arrival horizon."""
        if due is None:
            return
        if self._arrival_until_us is not None and due > self._arrival_until_us:
            return
        self._heap.push(due, "arrival", {"index": self._arrival.state.next_index - 1})

    def _schedule_first_arrival(self) -> None:
        self._schedule_arrival(self._next_arrival_us())

    def _handle_arrival(self, timer: Timer) -> list[InternalEnvelope]:
        v = timer.virtual_due_at
        actor_key = self._bind_actor(int(timer.ref.get("index", 0)))
        if actor_key is not None:
            self._start_session(actor_key, v)
        # schedule the next arrival immediately (§3.5 step 3).
        self._schedule_arrival(self._next_arrival_us())
        return []  # the session's first event comes from its own dwell timer

    def _bind_actor(self, arrival_index: int) -> str | None:
        """BE-A1: index i₀ = ⌊u.N⌋ then circular scan to first eligible actor."""
        registry = self._pools.pool(self._ir.actor_entity).creation_order
        n = len(registry)
        if n == 0:
            return None
        bind_cursor = Cursor(
            self._tree.key("transitions", f"bind:{self._config.shard_id}:{arrival_index}")
        )
        i0 = int(bind_cursor.u64() % n)
        for offset in range(n):
            key = registry[(i0 + offset) % n]
            record = self._pools.get(self._ir.actor_entity, key)
            if record is not None and record.status == "live" and not record.in_session:
                return key
        return None  # all in session ⇒ deterministic drop (BE-A3)

    def _start_session(self, actor_key: str, v: int) -> None:
        machine = self._ir.machines[self._ir.session_machine]
        # session_id minted from values:arrival:{shard}:{n} (§7.1), ts = arrival v.
        n = self._arrival.state.next_index - 1
        sid_cursor = Cursor(self._tree.key("values", f"arrival:{self._config.shard_id}:{n}"))
        occurred_at = self._vclock.instant_for(v)
        session_id = event_id_for(occurred_at, UuidBits(sid_cursor))
        rng = traversal_rng(
            self._tree, transitions_ctx=f"session:{session_id}",
            values_ctx=f"session:{session_id}",
        )
        actor = self._pools.get(self._ir.actor_entity, actor_key)
        if actor is not None:
            actor.in_session = True
        traversal = Traversal(
            traversal_id=session_id, machine=self._ir.session_machine, kind="session",
            state=machine.initial, actor_key=actor_key, subject_type=None,
            subject_key=None, rng=rng, correlation_id="", last_event_id=None,
            spawned_at_us=v, session_id=session_id,
        )
        self._interp.traversals[session_id] = traversal
        # session_timeout backstop (BE-A5).
        if machine.session_timeout_us is not None:
            self._heap.push(
                v + machine.session_timeout_us, "session_timeout",
                {"traversal_id": session_id},
            )
        self._interp.schedule_initial(traversal, v)

    # -- the generate call (§3.3) ------------------------------------------

    def generate(self, budget: int, until_us: int) -> list[InternalEnvelope]:
        """Process due timers up to ``until_us``, bounded by ``budget`` tokens.

        Headroom is checked BEFORE interpreting (≥ 9), so a transaction is never
        split or re-run (§3.3). ``F`` advances on processing only (BE-C2/C3).
        """
        batch: list[InternalEnvelope] = []
        emitted_at = self._clock_port.now()
        while True:
            top = self._heap.peek()
            if top is None or top.virtual_due_at > until_us:
                break
            if budget - len(batch) < MAX_EVENTS_PER_TX:
                break
            timer = self._heap.pop()
            self._vclock.advance_frontier(timer.virtual_due_at)
            if timer.kind == "arrival":
                batch.extend(self._handle_arrival(timer))
            elif timer.kind == "background_day":
                batch.extend(self._handle_background_day(timer))
            elif timer.kind == "bg_mutation":
                batch.extend(self._handle_bg_mutation(timer, emitted_at))
            else:
                batch.extend(self._interp.interpret(timer, emitted_at=emitted_at))
        return batch

    # -- batch / backfill core (§8) ----------------------------------------

    def run_batch(
        self,
        *,
        max_events: int | None = None,
        until_us: int | None = None,
        ledger: LedgerSink | None = None,
        pass_size: int = 500,
        arrival_until_us: int | None = None,
    ) -> list[InternalEnvelope]:
        """Unpaced generation to completion (backfill / batch finalization, §8).

        Seeds (head ``op:"r"`` rows), then drains the heap in memory-bounded passes
        of ``pass_size`` events until the heap is empty, ``until_us`` is crossed
        (window end, BE-F3), or ``max_events`` is reached. Appends each pass to the
        ``ledger`` durably before returning (INV-GEN-5) when a sink is supplied.

        ``arrival_until_us`` (optional) stops scheduling *new* arrivals once the
        arrival chain crosses that simulated-µs horizon while ``until_us`` still drains
        every spawned lifecycle to completion — the "fixed session population, full
        lifecycle maturity" shape of the testing-strategy §5.2 funnel/latency batch A
        (50k sessions whose lifecycles fully resolve, so realized parent→child ratios
        are not understated by window-edge truncation). Defaults to ``None`` (arrivals
        run for the whole window), so backfill datasets are unaffected.
        """
        self._arrival_until_us = arrival_until_us
        window_end = until_us if until_us is not None else _MAX_VIRTUAL_US
        produced: list[InternalEnvelope] = []
        head = self.seed()
        if head:
            self._append(head, ledger)
            produced.extend(head)
        while True:
            top = self._heap.peek()
            if top is None or top.virtual_due_at > window_end:
                break
            remaining = (max_events - len(produced)) if max_events is not None else pass_size
            if remaining <= 0:
                break
            budget = min(pass_size, remaining)
            pass_batch = self.generate(budget, window_end)
            if not pass_batch:
                # no timer was due/affordable within budget but heap not drained:
                # advance by re-trying a larger budget if max not hit, else stop.
                if max_events is not None and len(produced) >= max_events:
                    break
                if self._heap.peek() is None:
                    break
                # token headroom only: bump pass to clear one transaction.
                pass_batch = self.generate(MAX_EVENTS_PER_TX, window_end)
                if not pass_batch:
                    break
            self._append(pass_batch, ledger)
            produced.extend(pass_batch)
            if max_events is not None and len(produced) >= max_events:
                break
        return produced

    def _append(self, batch: list[InternalEnvelope], ledger: LedgerSink | None) -> None:
        if ledger is not None and batch:
            ledger.append(batch)


_MAX_VIRTUAL_US = (1 << 62) - 1
