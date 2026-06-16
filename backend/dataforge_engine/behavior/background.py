"""Background mutations — manifest-declared attribute drift with no business
cause (event-model R-CDC-3; R-CDC-M2; behavior-engine §6.1).

A ``cdc.entities.*.background_mutations`` rule mutates each eligible pooled
entity with ``probability_per_day`` per simulated day (``per: entity_day``).
There is **no causing business event**: every emitted CDC event is a *chain
root* — ``causation_id`` null, ``correlation_id = event_id``, ``actor_id`` null,
``source.tx_id`` null (R-CDC-3). The transaction.py CDC view already routes the
``tx_id is None and correlation_id == ""`` case to the chain-root branch; this
driver is the producer that opens those CDC-only :class:`PoolTransaction`\\ s.

The schedule is deterministic and state-first: one ``background_day`` sweep timer
per simulated day, processed in heap order like any other timer. The per-(entity,
rule, day) Bernoulli draw and the within-day offset are keyed off the ``pools``
sub-seed, so the same seed yields byte-identical CDC drift (GOLD-B). Only ``u``
ops are produced (drift on an existing live row); ``c``/``r``/``d`` come from
effects / seeding (R-CDC-4: never a ``u`` before the entity's ``c``/``r``).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dataforge_engine.envelope import event_id_for, format_rfc3339

from .evaluate import resolve_set
from .rng import Cursor, TraversalRng, UuidBits
from .runtime import BindingContext, Traversal
from .transaction import Mutation, PoolTransaction

if TYPE_CHECKING:
    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.seeds import SeedTree

    from .clock import VirtualClock
    from .ir import ManifestIR
    from .pools import EntityPools, PooledEntity
    from .transaction import SequenceCounter, StreamIdentity

# One simulated day in virtual µs (the ``entity_day`` rate basis, R-CDC-M2).
US_PER_DAY = 86_400_000_000

# Stable, traversal-less binding the resolver sees: background-mutation ``set``
# generators are context-free (no ``from`` paths), so a placeholder traversal id
# suffices — the cursor passed to ``resolve_set`` carries all determinism.
_BG_TRAVERSAL_ID = "__background__"

# A never-drawn placeholder RNG for the traversal-less background context: the
# real draws come from the per-(entity, rule, day) ``pools`` cursor, not here.
_NULL_RNG = TraversalRng(Cursor(b"\x00" * 32), Cursor(b"\x00" * 32))


class BackgroundMutationDriver:
    """Drives manifest-declared background mutations as CDC-only chain roots.

    The shard schedules one ``background_day`` timer per simulated day; on each
    sweep this driver walks every CDC-enabled entity that has background-mutation
    rules and, per live entity instance, fires each rule with its per-day
    probability. Firing applies the ``set`` drift to the pooled record (bumping
    ``entity_version`` by exactly 1, R-CDC-5 gapless) and emits one ``cdc.{entity}``
    ``u`` event whose chain root is its own ``event_id`` (R-CDC-3).
    """

    def __init__(
        self,
        ir: ManifestIR,
        pools: EntityPools,
        clock: VirtualClock,
        tree: SeedTree,
        identity: StreamIdentity,
        sequence: SequenceCounter,
    ) -> None:
        self._ir = ir
        self._pools = pools
        self._clock = clock
        self._tree = tree
        self._id = identity
        self._seq = sequence
        # entity types with at least one rule, in declaration order (R-CDC-2 order).
        # Background mutations are a Phase-8 behavior (R-CDC-3): a pre-1.1.0 manifest
        # that forward-declares rules (the 1.0.0 subset does) stays inert so its
        # golden baseline — frozen before this driver existed — never gains new CDC
        # chain-root events (behavior-engine §3.4/§8; gated on ManifestIR.phase8_features).
        self._active: tuple[str, ...] = (
            tuple(
                name
                for name in ir.entity_order
                if (e := ir.entities[name]).cdc_enabled and e.background_mutations
            )
            if ir.phase8_features
            else ()
        )

    @property
    def has_rules(self) -> bool:
        return bool(self._active)

    def plan_day(self, day_index: int) -> list[tuple[int, dict[str, str | int]]]:
        """Decide which entities drift on simulated day ``day_index``.

        Returns ``(due_us, ref)`` pairs for the shard to push as ``bg_mutation``
        timers — one per firing — so each drift event is processed in virtual-time
        order and bounded to a single CDC envelope per heap pop (never a monolithic
        many-row batch). The per-(entity, rule, instance, day) Bernoulli is drawn
        at cursor position 0; the within-day offset at position 1; both are
        re-derivable at fire time from the same ``pools`` key (cursor draws are
        position-addressable, so planning and firing stay byte-identical, GOLD-B).

        Eligibility is the pool state at plan time (live keys, creation order); an
        entity that turns terminal/deleted before its timer fires is skipped then.
        """
        planned: list[tuple[int, dict[str, str | int]]] = []
        for entity_type in self._active:
            entity_ir = self._ir.entities[entity_type]
            for key in self._pools.live_keys(entity_type):
                record = self._pools.get(entity_type, key)
                if record is None or record.status != "live":
                    continue
                for rule_idx in range(len(entity_ir.background_mutations)):
                    rule = entity_ir.background_mutations[rule_idx]
                    cursor = self._rule_cursor(entity_type, rule_idx, key, day_index)
                    if cursor.u() >= rule.probability_per_day:
                        continue  # deterministic miss for this entity-day
                    offset_us = int(cursor.u() * US_PER_DAY)
                    due = day_index * US_PER_DAY + offset_us
                    planned.append((due, {
                        "entity_type": entity_type, "entity_key": key,
                        "rule_idx": rule_idx, "day_index": day_index,
                    }))
        return planned

    def fire(self, ref: dict[str, Any], *, emitted_at: object) -> list[InternalEnvelope]:
        """Emit the CDC ``u`` for one planned firing (a ``bg_mutation`` timer).

        Re-derives the same offset + drift values from the ``pools`` cursor, applies
        the drift, bumps ``entity_version`` by 1 (R-CDC-5 gapless), and commits a
        CDC-only :class:`PoolTransaction` (chain root, R-CDC-3). Returns ``[]`` if
        the entity is no longer live (it terminated after planning).
        """
        from datetime import datetime

        assert isinstance(emitted_at, datetime)
        entity_type = str(ref["entity_type"])
        entity_key = str(ref["entity_key"])
        rule_idx = int(ref["rule_idx"])
        day_index = int(ref["day_index"])
        record = self._pools.get(entity_type, entity_key)
        if record is None or record.status != "live":
            return []  # terminated between planning and firing — no event
        rule = self._ir.entities[entity_type].background_mutations[rule_idx]
        cursor = self._rule_cursor(entity_type, rule_idx, entity_key, day_index)
        cursor.u()  # position 0: Bernoulli (re-skip; we already know it fired)
        offset_us = int(cursor.u() * US_PER_DAY)  # position 1: within-day offset
        v = day_index * US_PER_DAY + offset_us
        occurred_at = self._clock.instant_for(v)
        now_iso = format_rfc3339(occurred_at)

        before = record.row_image()
        ctx = self._context(record, now_iso)
        values = resolve_set(rule.set_sources, ctx, cursor, self._pools)
        record.attributes.update(values)
        record.entity_version += 1  # gapless per-entity total order (R-CDC-5)
        record.updated_at = now_iso

        event_id = event_id_for(occurred_at, UuidBits(cursor))
        # CDC-only transaction: no business event ⇒ commit() leaves ``tx_id`` None
        # and ``correlation_id`` "", routing _build_cdc to the chain-root branch
        # (causation_id/actor_id/session_id null, correlation_id = event_id). R-CDC-3.
        tx = PoolTransaction(
            self._ir, self._id, occurred_at=occurred_at, emitted_at=emitted_at
        )
        tx.record_mutation(Mutation(
            entity_type=entity_type, entity_key=entity_key, op="u",
            before=before, after=record.row_image(),
            entity_version=record.entity_version, event_id=event_id,
        ))
        return tx.commit(self._seq)

    def _rule_cursor(
        self, entity_type: str, rule_idx: int, entity_key: str, day_index: int
    ) -> Cursor:
        """The ``pools`` draw stream for one (entity, rule, instance, day).

        Keyed off the exact coordinate so the draw is independent of arrival order
        and identical across plan/fire and across restarts (state-first, GOLD-B).
        """
        ctx_key = f"bg:{entity_type}:{rule_idx}:{entity_key}:{day_index}"
        return Cursor(self._tree.key("pools", ctx_key))

    def _context(self, record: PooledEntity, now_iso: str) -> BindingContext:
        """A minimal traversal-less binding for the rule's context-free generators.

        The drifting record is bound as ``subject`` so a future ``ref.attr``/path
        generator could resolve against it; v0 rules use only context-free
        generators (address.full, commerce.price, number.int).
        """
        traversal = Traversal(
            traversal_id=_BG_TRAVERSAL_ID, machine="", kind="lifecycle",
            state="", actor_key=None, subject_type=record.entity_type,
            subject_key=record.entity_key, rng=_NULL_RNG, correlation_id="",
            last_event_id=None,
        )
        return BindingContext(
            self._pools, actor=None, subject=record, traversal=traversal,
            now_iso=now_iso, virtual_epoch_ms=self._clock.virtual_epoch_ms,
        )
