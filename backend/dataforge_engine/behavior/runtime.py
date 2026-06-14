"""Traversal runtime state + binding-context resolution (behavior-engine §2, §5).

A :class:`Traversal` is one in-flight session or lifecycle walk: current state,
session working memory (``remember`` keys — the cart), the chain ids, the two RNG
cursors, and the transition counter (BE-A6 cap). The :class:`BindingContext`
resolves the context paths (``actor.*``, ``subject.*``, ``session.*``,
``created.*``) that payload ``from``, guard ``path``, and effect targets read
(R-EVT-3, §6.4).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import GenerationError, TraversalCapExceeded

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

    from .pools import EntityPools, PooledEntity
    from .rng import TraversalRng

# BE-A6: traversal hard cap (defense line behind MAN-V207).
TRAVERSAL_HARD_CAP = 10_000


@dataclass
class Traversal:
    """One session or lifecycle traversal (behavior-engine §2.1)."""

    traversal_id: str  # session_id or "{machine}:{subject_key}"
    machine: str
    kind: str  # "session" | "lifecycle"
    state: str
    actor_key: str | None
    subject_type: str | None
    subject_key: str | None
    rng: TraversalRng
    correlation_id: str
    last_event_id: str | None
    memory: dict[str, JSONValue] = field(default_factory=dict)
    spawned_at_us: int = 0
    transition_count: int = 0
    session_id: str | None = None
    # The selection decided at scheduling time (§2.3 rule 4: selection + dwell
    # sampled together). -1 = remainder selected; None = not yet scheduled.
    pending_transition_idx: int | None = None

    def bump(self) -> None:
        self.transition_count += 1
        if self.transition_count > TRAVERSAL_HARD_CAP:
            raise TraversalCapExceeded(
                f"traversal {self.traversal_id} exceeded {TRAVERSAL_HARD_CAP} "
                "transitions (BE-A6)"
            )


class BindingContext:
    """Resolves context paths against a traversal's binding (R-EVT-3, §6.4).

    ``created`` holds entities created by the firing transition's effects (keyed
    by entity type). The resolver walks attribute segments and list markers
    (``session.cart_items[].unit_price``) the same way the manifest path grammar
    parses them.
    """

    def __init__(
        self,
        pools: EntityPools,
        *,
        actor: PooledEntity | None,
        subject: PooledEntity | None,
        traversal: Traversal,
        now_iso: str,
        virtual_epoch_ms: int,
    ) -> None:
        self._pools = pools
        self._actor = actor
        self._subject = subject
        self._traversal = traversal
        self.created: dict[str, PooledEntity] = {}
        self._now_iso = now_iso
        self._virtual_epoch_ms = virtual_epoch_ms

    def register_created(self, entity: PooledEntity) -> None:
        self.created[entity.entity_type] = entity

    # -- entity-ref resolution (effect targets, partition_by) --------------

    def resolve_entity_ref(self, ref: str) -> tuple[str, PooledEntity]:
        """Resolve an ``entityRef`` (``actor``/``subject``/``created.x`` + ``.via.``)."""
        from dataforge_engine.manifest.paths import parse_entity_ref

        parsed = parse_entity_ref(ref)
        record = self._root_record(parsed.kind, parsed.created_entity)
        for rel in parsed.via:
            record = self._hop(record, rel)
        return record.entity_type, record

    def _root_record(self, kind: str, created_entity: str | None) -> PooledEntity:
        if kind == "actor":
            if self._actor is None:
                raise GenerationError("context has no actor")
            return self._actor
        if kind == "subject":
            if self._subject is None:
                raise GenerationError("context has no subject")
            return self._subject
        if kind == "created":
            assert created_entity is not None
            rec = self.created.get(created_entity)
            if rec is None:
                raise GenerationError(f"no created.{created_entity} in context")
            return rec
        raise GenerationError(f"cannot root an entity ref at {kind!r}")

    def _hop(self, record: PooledEntity, relationship: str) -> PooledEntity:
        # Follow source→target: the record holds the FK attribute pointing at the
        # relationship's target.
        target_type = self._pools.relationship_target(relationship)
        src_attr = self._fk_attribute(relationship)
        target_key = record.attributes.get(src_attr)
        if not isinstance(target_key, str):
            raise GenerationError(f".via.{relationship}: no fk on {record.entity_key}")
        return self._pools.require(target_type, target_key)

    def _fk_attribute(self, relationship: str) -> str:
        # relationship meta: (source_entity, source_attribute, target_entity)
        for name in (relationship,):
            meta = self._pools._rel_meta.get(name)
            if meta is not None:
                return meta[1]
        raise GenerationError(f"unknown relationship {relationship!r}")

    # -- context-path resolution (payload from, guard path, expr) ----------

    def resolve_path(self, path: str) -> JSONValue:
        """Resolve a ``contextPath`` to a value (scalar or list)."""
        from dataforge_engine.manifest.paths import parse_context_path

        parsed = parse_context_path(path)
        if parsed.kind == "session":
            return self._resolve_session(parsed.segments)
        record = self._root_record(parsed.kind, parsed.created_entity)
        return self._walk_attributes(record, parsed.segments)

    def _walk_attributes(
        self, record: PooledEntity, segments: tuple[tuple[str, bool], ...]
    ) -> JSONValue:
        value: JSONValue = record.attributes.get(segments[0][0]) if segments else None
        if not segments:
            return None
        # First segment may itself be the key attribute (e.g. subject.order_id).
        first = segments[0][0]
        if first in record.attributes:
            value = record.attributes[first]
        for seg_name, _is_list in segments[1:]:
            if isinstance(value, dict):
                value = value.get(seg_name)
            else:
                raise GenerationError(f"cannot descend into {seg_name!r}")
        return value

    def _resolve_session(self, segments: tuple[tuple[str, bool], ...]) -> JSONValue:
        if not segments:
            return None
        key, _ = segments[0]
        mem = self._traversal.memory.get(key)
        if len(segments) == 1:
            return mem
        # session.key[].field → list of field values; session.key.field → scalar.
        second_name, second_is_list = segments[1]
        if segments[0][1] or second_is_list or isinstance(mem, list):
            items = mem if isinstance(mem, list) else []
            return [
                item.get(second_name) for item in items if isinstance(item, dict)
            ]
        if isinstance(mem, dict):
            return mem.get(second_name)
        return None

    def now_iso(self) -> str:
        return self._now_iso

    def virtual_epoch_ms(self) -> int:
        return self._virtual_epoch_ms
