"""The machine interpreter — the §2.3 per-timer algorithm (behavior-engine §2.3).

When the scheduler delivers a due timer for a traversal in state ``s`` at virtual
time ``v``, the interpreter executes exactly the §6.2 contract: timeout check →
select (one ``transitions`` draw, cumulative walk, remainder on ``u ≥ S``) → guard
(fall through to the remainder WITHOUT re-drawing on failure, BE-G2) → effects
(open a PoolTransaction; check-and-adjust atomic, §5.3) → emit (business event then
CDC, R-CDC-2) → advance (sample next dwell, push timer; terminal ends the
traversal).

The interpreter owns all RNG draws (cursors), all pool mutations, and all
envelope-id minting, so determinism is single-sourced. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from dataforge_engine.envelope import event_id_for

from .errors import GenerationError
from .evaluate import evaluate_guard, resolve_set, resolve_value_source
from .runtime import BindingContext, Traversal
from .transaction import Mutation, PoolTransaction, StreamIdentity

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.envelope.types import JSONValue
    from dataforge_engine.seeds import SeedTree

    from .clock import VirtualClock
    from .ir import Effect, EntityIR, EventTypeIR, ManifestIR, State, Transition
    from .observer import Observer
    from .pools import EntityPools, PooledEntity
    from .rng import Cursor
    from .scheduler import Timer, TimerHeap
    from .transaction import SequenceCounter


class Interpreter:
    """Executes transitions for one shard against shared pools + heap + IR."""

    def __init__(
        self,
        ir: ManifestIR,
        pools: EntityPools,
        heap: TimerHeap,
        identity: StreamIdentity,
        sequence: SequenceCounter,
        clock: VirtualClock,
        tree: SeedTree,
    ) -> None:
        self._ir = ir
        self._pools = pools
        self._heap = heap
        self._id = identity
        self._seq = sequence
        self._clock = clock
        self._tree = tree
        # traversal_id → Traversal (sessions + lifecycles).
        self.traversals: dict[str, Traversal] = {}
        # Optional dry-run instrumentation (plugin-arch §8.4). ``None`` on the
        # runner / golden hosts: the hot path then never touches it (BE-T1, no
        # cost, no behavior change). The L3 host attaches a recorder.
        self.observer: Observer | None = None

    # -- the §2.3 algorithm -------------------------------------------------

    def interpret(self, timer: Timer, *, emitted_at: datetime) -> list[InternalEnvelope]:
        """Process one due timer; return its canonical batch (may be empty)."""
        traversal = self.traversals.get(str(timer.ref.get("traversal_id", "")))
        if traversal is None:
            return []  # lazy cancellation: referent gone (§3.2)
        v = timer.virtual_due_at
        machine = self._ir.machines[traversal.machine]
        state = machine.states[traversal.state]
        occurred_at = self._clock.instant_for(v)

        # Step 1: timeout edge fired (it beat the sampled dwell, §2.3 rule 1).
        if timer.kind in ("state_timeout", "session_timeout"):
            return self._handle_timeout(traversal, state, timer, v, occurred_at, emitted_at)

        # Step 2: the selection was decided at scheduling time (rule 4) — no redraw.
        idx = traversal.pending_transition_idx
        traversal.pending_transition_idx = None
        transition = state.transitions[idx] if idx is not None and idx >= 0 else None

        # Step 3: guard (fall through to remainder on failure, no re-draw, BE-G2).
        if transition is not None:
            ctx = self._make_context(traversal, occurred_at, v)
            guard_ok = evaluate_guard(transition.guard, ctx, self._pools, v)
            if self.observer is not None and idx is not None and idx >= 0:
                self.observer.on_guard(
                    traversal.machine, traversal.state, idx, passed=guard_ok
                )
            if not guard_ok:
                transition = None  # remainder policy

        if transition is None:
            return self._apply_remainder(traversal, state, v, occurred_at, emitted_at)

        # Steps 4-6.
        return self._fire(traversal, state, transition, v, occurred_at, emitted_at)

    def schedule_initial(self, traversal: Traversal, v: int) -> None:
        """Schedule the first timer for a freshly-spawned traversal (arrival/spawn).

        The §2.3 model processes the ``initial`` state's first selection by sampling
        it here (selection + dwell together) and pushing the dwell timer.
        """
        machine = self._ir.machines[traversal.machine]
        self._schedule_next(traversal, machine.states[machine.initial], v)

    def _apply_remainder(
        self, traversal: Traversal, state: State, v: int,
        occurred_at: datetime, emitted_at: datetime,
    ) -> list[InternalEnvelope]:
        if state.remainder == "stay":
            # re-enter s, schedule a fresh re-evaluation dwell (§6.2 rule 1 stay).
            self._schedule_next(traversal, state, v)
            return []
        self._end_traversal(traversal)  # exit absorption (BE-A5) — no event
        return []

    def _fire(
        self, traversal: Traversal, state: State, transition: Transition,
        v: int, occurred_at: datetime, emitted_at: datetime,
    ) -> list[InternalEnvelope]:
        traversal.bump()
        tx = PoolTransaction(self._ir, self._id, occurred_at=occurred_at, emitted_at=emitted_at)
        ctx = self._make_context(traversal, occurred_at, v)
        try:
            self._run_effects(transition.effects, ctx, traversal, tx, occurred_at)
        except GenerationError:
            # check-and-adjust atomicity failure ⇒ abort, fall to remainder (BE-G5).
            return self._apply_remainder(traversal, state, v, occurred_at, emitted_at)

        if transition.emit is not None:
            self._stamp_business(transition.emit, traversal, ctx, tx, occurred_at)

        batch = tx.commit(self._seq)
        if batch:
            traversal.last_event_id = batch[0]["event_id"]
        self._advance(traversal, transition.to, v)
        self._spawn_lifecycles(ctx, v, traversal)
        return batch

    # -- effects (§6.4, §5.3) ----------------------------------------------

    def _run_effects(
        self, effects: tuple[Effect, ...], ctx: BindingContext,
        traversal: Traversal, tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        for effect in effects:
            if effect.action == "create":
                self._effect_create(effect, ctx, traversal, tx, occurred_at)
            elif effect.action == "update":
                self._effect_update(effect, ctx, traversal, tx, occurred_at)
            elif effect.action == "adjust":
                self._effect_adjust(effect, ctx, traversal, tx, occurred_at)
            elif effect.action == "delete":
                self._effect_delete(effect, ctx, traversal, tx, occurred_at)
            elif effect.action == "remember":
                self._effect_remember(effect, ctx, traversal)

    def _effect_create(
        self, effect: Effect, ctx: BindingContext, traversal: Traversal,
        tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        assert effect.entity is not None
        entity_ir = self._ir.entities[effect.entity]
        cursor = traversal.rng.values
        # explicit set values first (declaration order), then unset declared attrs.
        explicit = resolve_set(
            effect.set_sources, ctx, cursor, self._pools,
            strip_markers=self._ir.phase8_features,
        )
        attributes = self._generate_attributes(entity_ir, ctx, cursor, explicit)
        key = self._mint_key(entity_ir.name, entity_ir.key_prefix, cursor)
        attributes[entity_ir.key_attribute] = key
        now_iso = ctx.now_iso()
        from .pools import PooledEntity
        record = PooledEntity(
            entity_key=key, entity_type=entity_ir.name, attributes=attributes,
            entity_version=1, created_at=now_iso, updated_at=now_iso,
        )
        self._pools.insert(record)
        ctx.register_created(record)
        tx.record_mutation(Mutation(
            entity_type=entity_ir.name, entity_key=key, op="c",
            before=None, after=record.row_image(), entity_version=1,
            event_id=self._mint_event_id(traversal, occurred_at),
        ))

    def _effect_update(
        self, effect: Effect, ctx: BindingContext, traversal: Traversal,
        tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        assert effect.target is not None
        _t, record = ctx.resolve_entity_ref(effect.target)
        before = record.row_image()
        values = resolve_set(
            effect.set_sources, ctx, traversal.rng.values, self._pools,
            strip_markers=self._ir.phase8_features,
        )
        record.attributes.update(values)
        record.entity_version += 1
        record.updated_at = ctx.now_iso()
        tx.record_mutation(Mutation(
            entity_type=record.entity_type, entity_key=record.entity_key, op="u",
            before=before, after=record.row_image(), entity_version=record.entity_version,
            event_id=self._mint_event_id(traversal, occurred_at),
        ))

    def _effect_adjust(
        self, effect: Effect, ctx: BindingContext, traversal: Traversal,
        tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        assert effect.target is not None and effect.attribute is not None
        _t, record = ctx.resolve_entity_ref(effect.target)
        before = record.row_image()
        delta = (
            Decimal(str(effect.by_const)) if effect.by_const is not None
            else _as_decimal(ctx.resolve_path(effect.by_path or ""))
        )
        current = _as_decimal(record.attributes.get(effect.attribute, 0))
        new_value = current + delta
        # §5.3 check-and-adjust: a guarded numeric attribute may never go below its
        # implied floor (inventory ≥ 0). Atomicity failure ⇒ GenerationError ⇒
        # transaction aborts (BE-G5/G6).
        if new_value < 0:
            raise GenerationError(
                f"adjust would take {effect.attribute} below 0 (BE-G6)"
            )
        record.attributes[effect.attribute] = _normalize_number(new_value)
        record.entity_version += 1
        record.updated_at = ctx.now_iso()
        tx.record_mutation(Mutation(
            entity_type=record.entity_type, entity_key=record.entity_key, op="u",
            before=before, after=record.row_image(), entity_version=record.entity_version,
            event_id=self._mint_event_id(traversal, occurred_at),
        ))

    def _effect_delete(
        self, effect: Effect, ctx: BindingContext, traversal: Traversal,
        tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        assert effect.target is not None
        _t, record = ctx.resolve_entity_ref(effect.target)
        before = record.row_image()
        record.entity_version += 1
        record.status = "deleted"
        tx.record_mutation(Mutation(
            entity_type=record.entity_type, entity_key=record.entity_key, op="d",
            before=before, after=None, entity_version=record.entity_version,
            event_id=self._mint_event_id(traversal, occurred_at),
        ))
        self._pools.remove(record.entity_type, record.entity_key)

    def _effect_remember(self, effect: Effect, ctx: BindingContext, traversal: Traversal) -> None:
        assert effect.key is not None
        value = resolve_set(
            effect.set_sources, ctx, traversal.rng.values, self._pools,
            strip_markers=self._ir.phase8_features,
        )
        if effect.mode == "append":
            existing = traversal.memory.setdefault(effect.key, [])
            if isinstance(existing, list):
                existing.append(value)
        else:
            traversal.memory[effect.key] = value

    # -- payload + ids ------------------------------------------------------

    def _stamp_business(
        self, event_type_name: str, traversal: Traversal, ctx: BindingContext,
        tx: PoolTransaction, occurred_at: datetime,
    ) -> None:
        et = self._ir.event_types[event_type_name]
        event_id = self._mint_event_id(traversal, occurred_at)
        causation_id = traversal.last_event_id
        payload = self._build_payload(et, ctx, traversal)
        part_type, part_record = ctx.resolve_entity_ref(et.partition_by)
        if traversal.correlation_id == "":
            traversal.correlation_id = event_id  # chain root (C-1)
            causation_id = None
        entity_refs = self._build_entity_refs(part_type, part_record, payload, et, ctx)
        tx.set_business_event(
            event_type=event_type_name, event_id=event_id,
            partition_entity_type=part_type, partition_entity_key=part_record.entity_key,
            actor_id=traversal.actor_key, session_id=traversal.session_id,
            entity_refs=entity_refs, correlation_id=traversal.correlation_id,
            causation_id=causation_id, payload=payload,
        )

    def _build_payload(
        self, et: EventTypeIR, ctx: BindingContext, traversal: Traversal
    ) -> dict[str, JSONValue]:
        cursor = traversal.rng.values
        payload: dict[str, JSONValue] = {}
        ref_keys: dict[str, tuple[str, str]] = {}
        for fname, source in et.payload:
            value = resolve_value_source(
                source, ctx, cursor, self._pools, siblings=payload, ref_keys=ref_keys
            )
            payload[fname] = value
        payload.pop("__now__", None)
        payload.pop("__virtual_epoch_ms__", None)
        return payload

    def _build_entity_refs(
        self, part_type: str, part_record: PooledEntity,
        payload: dict[str, JSONValue], et: EventTypeIR, ctx: BindingContext,
    ) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = [
            {"entity_type": part_type, "entity_key": part_record.entity_key}
        ]
        seen = {(part_type, part_record.entity_key)}
        for fname, source in et.payload:
            if source.kind == "from" and source.path:
                from dataforge_engine.manifest.paths import parse_context_path
                parsed = parse_context_path(source.path)
                if parsed.created_entity and (val := payload.get(fname)):
                    pair = (parsed.created_entity, str(val))
                    if pair not in seen and isinstance(val, str):
                        refs.append({"entity_type": pair[0], "entity_key": pair[1]})
                        seen.add(pair)
        return refs[:16]

    def _mint_event_id(self, traversal: Traversal, occurred_at: datetime) -> str:
        return event_id_for(occurred_at, traversal.rng.uuid_bits)

    # -- spawning / advancing ----------------------------------------------

    def _advance(self, traversal: Traversal, target: str, v: int) -> None:
        machine = self._ir.machines[traversal.machine]
        traversal.state = target
        target_state = machine.states[target]
        if target_state.terminal:
            self._end_traversal(traversal)
            self._mark_subject_terminal(traversal)
            return
        self._schedule_next(traversal, target_state, v)

    def _schedule_next(self, traversal: Traversal, state: State, v: int) -> None:
        """Decide the next selection + dwell together and push the timer (rule 4/6).

        One ``transitions`` draw selects the transition (or the remainder); the
        selected transition's ``dwell`` is sampled with one further draw. The
        chosen index is stored on the traversal so the firing step does NOT redraw
        (rule 3 fall-through, BE-G2). A state-level timeout edge competes (rule 5).
        """
        idx, dwell_us = self._sample_selection(traversal, state)
        traversal.pending_transition_idx = idx
        if (
            state.timeout is not None
            and (idx < 0 or dwell_us > state.timeout.after_us)
        ):
            self._heap.push(
                v + state.timeout.after_us, "state_timeout",
                {"traversal_id": traversal.traversal_id},
            )
            return
        self._heap.push(
            v + dwell_us, "dwell", {"traversal_id": traversal.traversal_id}
        )

    def _sample_selection(self, traversal: Traversal, state: State) -> tuple[int, int]:
        """Return ``(transition_index, dwell_us)``; index -1 = remainder selected."""
        if not state.transitions:
            if self.observer is not None:
                self.observer.on_select(traversal.machine, state.name, -1)
            return -1, 0
        u = traversal.rng.transitions.u()
        for i, transition in enumerate(state.transitions):
            if u < transition.cumulative:
                if self.observer is not None:
                    self.observer.on_select(traversal.machine, state.name, i)
                dwell = transition.dwell
                if not dwell.needs_draw:
                    return i, dwell.sample_fixed_value()
                return i, dwell.sample(traversal.rng.transitions.u())
        if self.observer is not None:  # u ≥ S ⇒ remainder selected
            self.observer.on_select(traversal.machine, state.name, -1)
        return -1, 0  # u ≥ S ⇒ remainder (immediate; absorbed/re-entered at firing)

    def _spawn_lifecycles(self, ctx: BindingContext, v: int, parent: Traversal) -> None:
        for entity_type, record in ctx.created.items():
            machine_name = self._ir.lifecycle_by_entity.get(entity_type)
            if machine_name is None:
                continue
            self._spawn_lifecycle(machine_name, record, v, parent)

    def _spawn_lifecycle(
        self, machine_name: str, subject: PooledEntity, v: int, parent: Traversal
    ) -> None:
        from .rng import traversal_rng
        machine = self._ir.machines[machine_name]
        tid = f"{machine_name}:{subject.entity_key}"
        if tid in self.traversals:
            return  # exactly one traversal per (machine, entity instance)
        ctx_key = f"lifecycle:{machine_name}:{subject.entity_key}"
        rng = traversal_rng(self._tree, transitions_ctx=ctx_key, values_ctx=ctx_key)
        traversal = Traversal(
            traversal_id=tid, machine=machine_name, kind="lifecycle",
            state=machine.initial, actor_key=parent.actor_key,
            subject_type=subject.entity_type, subject_key=subject.entity_key,
            rng=rng, correlation_id=parent.correlation_id,
            last_event_id=parent.last_event_id, spawned_at_us=v, session_id=None,
        )
        self.traversals[tid] = traversal
        initial_state = machine.states[machine.initial]
        self._schedule_next(traversal, initial_state, v)

    def _end_traversal(self, traversal: Traversal) -> None:
        if traversal.kind == "session" and traversal.actor_key is not None:
            actor = self._pools.get(self._ir.actor_entity, traversal.actor_key)
            if actor is not None:
                actor.in_session = False
        self.traversals.pop(traversal.traversal_id, None)
        if self.observer is not None and traversal.kind == "session":
            self.observer.on_session_complete(traversal.traversal_id)

    def _mark_subject_terminal(self, traversal: Traversal) -> None:
        if traversal.subject_type and traversal.subject_key:
            record = self._pools.get(traversal.subject_type, traversal.subject_key)
            if record is not None and record.status == "live":
                record.status = "terminal"

    def _handle_timeout(
        self, traversal: Traversal, state: State, timer: Timer, v: int,
        occurred_at: datetime, emitted_at: datetime,
    ) -> list[InternalEnvelope]:
        if timer.kind == "session_timeout":
            self._end_traversal(traversal)  # absorbed, no event (BE-A5)
            return []
        if state.timeout is None:
            return []
        edge = state.timeout
        traversal.bump()
        tx = PoolTransaction(self._ir, self._id, occurred_at=occurred_at, emitted_at=emitted_at)
        ctx = self._make_context(traversal, occurred_at, v)
        self._run_effects(edge.effects, ctx, traversal, tx, occurred_at)
        if edge.emit is not None:
            self._stamp_business(edge.emit, traversal, ctx, tx, occurred_at)
        batch = tx.commit(self._seq)
        if batch:
            traversal.last_event_id = batch[0]["event_id"]
        self._advance(traversal, edge.to, v)
        self._spawn_lifecycles(ctx, v, traversal)
        return batch

    # -- helpers ------------------------------------------------------------

    def _make_context(
        self, traversal: Traversal, occurred_at: datetime, v: int
    ) -> BindingContext:
        from dataforge_engine.envelope import format_rfc3339
        actor = (
            self._pools.get(self._ir.actor_entity, traversal.actor_key)
            if traversal.actor_key else None
        )
        subject = (
            self._pools.get(traversal.subject_type, traversal.subject_key)
            if traversal.subject_type and traversal.subject_key else None
        )
        return BindingContext(
            self._pools, actor=actor, subject=subject, traversal=traversal,
            now_iso=format_rfc3339(occurred_at),
            virtual_epoch_ms=self._clock.virtual_epoch_ms,
        )

    def _generate_attributes(
        self, entity_ir: EntityIR, ctx: BindingContext, cursor: Cursor,
        explicit: dict[str, JSONValue],
    ) -> dict[str, JSONValue]:
        from .generators import GenContext
        attributes: dict[str, JSONValue] = {}
        ref_keys: dict[str, tuple[str, str]] = {}
        for attr_name, gen in entity_ir.attributes:
            if attr_name in explicit:
                attributes[attr_name] = explicit[attr_name]
                continue
            gctx = GenContext(
                siblings=attributes, pools=self._pools, ref_keys=ref_keys,
                expr_resolver=ctx.resolve_path,
            )
            gctx.siblings.setdefault("__now__", ctx.now_iso())
            gctx.siblings.setdefault("__virtual_epoch_ms__", ctx.virtual_epoch_ms())
            value = gen(cursor, gctx)
            attributes[attr_name] = value
            rel = getattr(gen, "__df_relationship__", None)
            if rel is not None and isinstance(value, str):
                ref_keys[attr_name] = (self._pools.relationship_target(rel), value)
        attributes.pop("__now__", None)
        attributes.pop("__virtual_epoch_ms__", None)
        # explicit-only attrs not declared (none in v0) are ignored.
        for name, value in explicit.items():
            attributes.setdefault(name, value)
        return attributes

    def _mint_key(self, entity_type: str, prefix: str, cursor: Cursor) -> str:
        hex_value = cursor.u64()
        body = self._pools.next_key_hex(entity_type, hex_value)
        return f"{prefix}_{body}"


def _as_decimal(value: JSONValue) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise GenerationError(f"adjust operand not numeric: {value!r}")


def _normalize_number(value: Decimal) -> JSONValue:
    if value == value.to_integral_value():
        return int(value)
    return value
