"""Entity pools — the per-stream, per-entity-type populations every event derives
from (behavior-engine §4; ADR-0007/ADR-0012).

Tier 1 (this module) is the **authoritative** in-process working set the
interpreter reads and mutates: a ``dict`` per entity type (``entity_key →
PooledEntity``), relationship indexes for O(1) ``exists`` guards and ``ref.fk``
selection, and the append-only actor registry (BE-A1). Tier-2 Redis and Tier-3
snapshots are write-behind seams reached through :mod:`dataforge_engine.ports`.

Key invariants enforced here:

* ``entity_version`` increments by exactly 1 on **every** mutation, from day one
  (so Phase 8 CDC images derive without rework — behavior-engine §6).
* ``created_at`` / ``updated_at`` maintained on every pooled entity (§4.2).
* terminal-entity archival (§4.4) keeps the live set under the B-09 cap.

Indexes are **derived state**: never checkpointed, always rebuilt from the loaded
pool image on restore (§4.2). Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from .errors import GenerationError

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

# B-09: runtime live working set per entity type per stream.
LIVE_WORKING_SET_CAP = 500_000

EntityStatus = Literal["live", "terminal", "deleted"]


@dataclass
class PooledEntity:
    """The Tier-1 record the interpreter holds (behavior-engine §4.2).

    ``attributes`` carries the declared attribute values (and ``key_attribute``);
    ``created_at``/``updated_at`` are runtime-maintained RFC-3339 *simulated*
    strings; ``entity_version`` is the authoritative per-entity total order.
    """

    entity_key: str
    entity_type: str
    attributes: dict[str, JSONValue]
    entity_version: int
    created_at: str
    updated_at: str
    status: EntityStatus = "live"
    in_session: bool = False  # actor entities only (BE-A2 eligibility)

    def row_image(self) -> dict[str, JSONValue]:
        """The full CDC row image (R-DER-1): declared attributes + auto timestamps.

        A *copy*, so the PoolTransaction captures before/after images that do not
        alias the live record (behavior-engine §6.1 step 1).
        """
        image: dict[str, JSONValue] = dict(self.attributes)
        image["created_at"] = self.created_at
        image["updated_at"] = self.updated_at
        return image

    def snapshot_json(self) -> dict[str, JSONValue]:
        """The JSON the Redis hash and snapshot JSONL serialize (§4.2)."""
        return {
            "entity_key": self.entity_key,
            "attributes": dict(self.attributes),
            "entity_version": self.entity_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "in_session": self.in_session,
        }


@dataclass
class _TypePool:
    """One entity type's working set + derived indexes."""

    entity_type: str
    records: dict[str, PooledEntity] = field(default_factory=dict)
    # Append-only creation-order key list (BE-A1 actor registry / ref.fk index).
    creation_order: list[str] = field(default_factory=list)
    # key_counter for entity-key hex generation (§4.5 step 2); checkpointed.
    key_counter: int = 0
    created_total: int = 0
    archived_total: int = 0


class EntityPools:
    """All Tier-1 pools for one (stream, shard), plus relationship indexes.

    The interpreter and generators hold one instance. Relationship indexes are
    keyed by relationship *name*: ``target_entity_key → set(source_entity_key)``
    for O(1) ``exists`` lookups and reverse traversals (behavior-engine §4.2).
    """

    def __init__(self, actor_entity: str) -> None:
        self._actor_entity = actor_entity
        self._pools: dict[str, _TypePool] = {}
        # relationship name → {target_key → set(source_key)}
        self._rel_fwd: dict[str, dict[str, set[str]]] = {}
        # relationship name → (source_entity, source_attribute, target_entity)
        self._rel_meta: dict[str, tuple[str, str, str]] = {}

    # -- registration -------------------------------------------------------

    def register_type(self, entity_type: str) -> None:
        if entity_type not in self._pools:
            self._pools[entity_type] = _TypePool(entity_type)

    def register_relationship(
        self, name: str, source_entity: str, source_attribute: str, target_entity: str
    ) -> None:
        self._rel_meta[name] = (source_entity, source_attribute, target_entity)
        self._rel_fwd.setdefault(name, {})

    # -- access -------------------------------------------------------------

    def pool(self, entity_type: str) -> _TypePool:
        return self._pools[entity_type]

    def get(self, entity_type: str, entity_key: str) -> PooledEntity | None:
        return self._pools[entity_type].records.get(entity_key)

    def require(self, entity_type: str, entity_key: str) -> PooledEntity:
        rec = self.get(entity_type, entity_key)
        if rec is None:
            raise GenerationError(f"missing pooled entity {entity_type}:{entity_key}")
        return rec

    def live_keys(self, entity_type: str) -> list[str]:
        """Creation-order keys whose record is still live (ref.fk selection set)."""
        pool = self._pools[entity_type]
        return [k for k in pool.creation_order
                if (r := pool.records.get(k)) is not None and r.status != "deleted"]

    def count(self, entity_type: str) -> int:
        return len(self._pools[entity_type].records)

    # -- mutation -----------------------------------------------------------

    def next_key_hex(self, entity_type: str, hex_value: int) -> str:
        """Render a u64 draw as the 16-hex key body; bump the type's key_counter.

        Collisions (negligible) are the caller's concern — it redraws at the bumped
        counter (§4.5 step 2). Returns the 16-char lowercase hex string.
        """
        pool = self._pools[entity_type]
        pool.key_counter += 1
        return f"{hex_value & 0xFFFFFFFFFFFFFFFF:016x}"

    def insert(self, record: PooledEntity) -> None:
        """Insert a freshly-created/seeded record and index it (CDC ``c``/``r``)."""
        pool = self._pools[record.entity_type]
        if record.entity_key in pool.records:
            raise GenerationError(
                f"duplicate entity key {record.entity_type}:{record.entity_key}"
            )
        pool.records[record.entity_key] = record
        pool.creation_order.append(record.entity_key)
        pool.created_total += 1
        self._index_relationships(record)
        self._enforce_cap(record.entity_type)

    def reindex_loaded(self, record: PooledEntity) -> None:
        """Insert a record loaded from a snapshot on restore (no created_total bump
        beyond the loaded count); rebuilds indexes (§9.3 step 2).
        """
        pool = self._pools[record.entity_type]
        pool.records[record.entity_key] = record
        pool.creation_order.append(record.entity_key)
        self._index_relationships(record)

    def remove(self, entity_type: str, entity_key: str) -> None:
        """Drop a record after its CDC ``d`` emits (or on archival, §4.4)."""
        pool = self._pools[entity_type]
        record = pool.records.pop(entity_key, None)
        if record is not None:
            self._deindex_relationships(record)

    # -- relationship indexing ---------------------------------------------

    def _index_relationships(self, record: PooledEntity) -> None:
        for name, (src_entity, src_attr, _tgt) in self._rel_meta.items():
            if src_entity != record.entity_type:
                continue
            target_key = record.attributes.get(src_attr)
            if isinstance(target_key, str):
                self._rel_fwd[name].setdefault(target_key, set()).add(record.entity_key)

    def _deindex_relationships(self, record: PooledEntity) -> None:
        for name, (src_entity, src_attr, _tgt) in self._rel_meta.items():
            if src_entity != record.entity_type:
                continue
            target_key = record.attributes.get(src_attr)
            if isinstance(target_key, str):
                sources = self._rel_fwd[name].get(target_key)
                if sources is not None:
                    sources.discard(record.entity_key)

    def sources_for(self, relationship: str, target_key: str) -> set[str]:
        """Source keys pointing at ``target_key`` via ``relationship`` (exists guard)."""
        return self._rel_fwd.get(relationship, {}).get(target_key, set())

    def relationship_target(self, relationship: str) -> str:
        return self._rel_meta[relationship][2]

    def relationship_source(self, relationship: str) -> str:
        return self._rel_meta[relationship][0]

    # -- archival (§4.4) ----------------------------------------------------

    def _enforce_cap(self, entity_type: str) -> None:
        """BE-E4: live non-terminal alone over cap ⇒ GenerationError (defense line)."""
        pool = self._pools[entity_type]
        if len(pool.records) <= LIVE_WORKING_SET_CAP:
            return
        non_terminal = sum(1 for r in pool.records.values() if r.status != "terminal")
        if non_terminal > LIVE_WORKING_SET_CAP:
            raise GenerationError(
                f"live non-terminal {entity_type} count {non_terminal} exceeds "
                f"the {LIVE_WORKING_SET_CAP} cap (BE-E4)"
            )

    def archive_eligible(
        self, entity_type: str, frontier_us: int, reachability_window_us: int,
        pending_refs: frozenset[str],
    ) -> list[str]:
        """Keys archivable at ``frontier_us`` (BE-E2): terminal, no pending timer,
        ``frontier - updated_at > reachability_window``. Caller supplies the
        ``updated_at`` µs lookup is via the record; ``pending_refs`` are keys with a
        live heap timer. Returns the eligible keys (archival is the caller's act).
        """
        pool = self._pools[entity_type]
        eligible: list[str] = []
        for key, rec in pool.records.items():
            if rec.status != "terminal" or key in pending_refs:
                continue
            # updated_at is RFC-3339; the caller passes the frontier already in µs
            # and compares against the record's stored µs via the clock helper. To
            # keep pools clock-free, age is computed by the caller; here we only
            # filter on status + pending. Window comparison is done by the sweep.
            eligible.append(key)
        return eligible

    def archive(self, entity_type: str, entity_key: str) -> None:
        """Remove an archived terminal entity from Tier-1 (§4.4)."""
        pool = self._pools[entity_type]
        if entity_key in pool.records:
            self.remove(entity_type, entity_key)
            pool.archived_total += 1
