"""Pool seeding from the manifest catalogs (behavior-engine §4.5).

First start of a stream seeds entity pools from ``seeding.catalogs`` sizes, in
manifest declaration order, ordinal ``0 … size-1`` within a type. Keys derive from
the ``pools`` sub-seed; attributes from the ``values`` sub-seed; every seeded
entity gets ``created_at = updated_at = virtual_epoch``, ``entity_version = 1``.
For every CDC-enabled seeded entity, exactly one ``op:"r"`` snapshot event is
emitted at the head of the stream (``occurred_at = virtual_epoch``), before any
arrival fires.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataforge_engine.envelope import event_id_for

from .generators import GenContext
from .partitioning import owns_key
from .pools import PooledEntity
from .rng import Cursor, UuidBits
from .transaction import Mutation, PoolTransaction, StreamIdentity

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.envelope.types import JSONValue
    from dataforge_engine.seeds import SeedTree

    from .clock import VirtualClock
    from .ir import EntityIR, ManifestIR
    from .pools import EntityPools
    from .transaction import SequenceCounter


def seed_pools(
    ir: ManifestIR,
    pools: EntityPools,
    tree: SeedTree,
    clock: VirtualClock,
    identity: StreamIdentity,
    sequence: SequenceCounter,
    *,
    emitted_at: datetime,
    overrides: dict[str, int] | None = None,
    shard_id: int = 0,
    shard_count: int = 1,
) -> list[InternalEnvelope]:
    """Seed all pools and return the head-of-stream ``op:"r"`` snapshot batch.

    ``overrides`` may set per-entity catalog sizes (instance-overridable within
    declared min/max, §4.5); absent keys use the manifest default.

    **Sharding (Phase 11).** Every shard registers + seeds the *full* catalog (each
    shard's pools must be complete for FK/relationship resolution), but a shard emits
    the head ``op:"r"`` snapshot only for the entity keys it **owns** under
    ``shard_for_key(entity_key) == shard_id`` — so across the ``shard_count`` shards
    each seeded entity's snapshot is emitted exactly once (no cross-shard duplication),
    and each shard's gapless ``sequence_no`` (INV-GEN-7) advances only over its owned
    snapshots. At ``shard_count == 1`` every key is owned, so the snapshot batch is the
    legacy single-shard head verbatim (byte-stable, GOLD-A).
    """
    overrides = overrides or {}
    epoch_iso = clock.instant_for(0)
    from dataforge_engine.envelope import format_rfc3339
    now_iso = format_rfc3339(epoch_iso)

    # 1. register types + relationships once.
    for name in ir.entity_order:
        pools.register_type(name)
    for rel_name, src, attr, tgt in ir.relationships:
        pools.register_relationship(rel_name, src, attr, tgt)

    # 2-4. seed entities in declaration order.
    for name in ir.entity_order:
        entity_ir = ir.entities[name]
        size = overrides.get(name, ir.seeding.get(name, 0))
        for ordinal in range(size):
            _seed_one(ir, entity_ir, pools, tree, ordinal, now_iso)

    # 5. snapshot reads for every CDC-enabled seeded instance, in seeding order.
    snapshots: list[InternalEnvelope] = []
    for name in ir.entity_order:
        entity_ir = ir.entities[name]
        if not entity_ir.cdc_enabled or "r" not in entity_ir.cdc_ops:
            continue
        size = overrides.get(name, ir.seeding.get(name, 0))
        if size == 0:
            continue
        snapshots.extend(
            _emit_snapshots(ir, entity_ir, pools, tree, identity, sequence,
                            occurred_at=epoch_iso, emitted_at=emitted_at,
                            shard_id=shard_id, shard_count=shard_count)
        )
    return snapshots


def _seed_one(
    ir: ManifestIR, entity_ir: EntityIR, pools: EntityPools, tree: SeedTree,
    ordinal: int, now_iso: str,
) -> None:
    key_cursor = Cursor(tree.key("pools", f"keys:{entity_ir.name}"), ordinal)
    value_cursor = Cursor(tree.key("values", f"entity:{entity_ir.name}:{ordinal}"))
    key_body = pools.next_key_hex(entity_ir.name, key_cursor.u64())
    key = f"{entity_ir.key_prefix}_{key_body}"

    # one_to_one FK source attributes seed to a *bijection*: equal-sized catalogs over
    # a one_to_one relationship pair the i-th source with the i-th target rather than
    # drawing with replacement, so every row resolves to exactly one counterpart
    # (behavior-engine §4.5; ecommerce.md §2 "seed catalogs are equal-sized so seeding
    # yields a bijection"). The draw is still consumed (the value cursor stays in
    # lockstep) — only the FK *value* is pinned, keeping every other seeded attribute
    # byte-stable.
    one_to_one = ir.one_to_one_seed_fks.get(entity_ir.name, {})

    attributes: dict[str, JSONValue] = {}
    ref_keys: dict[str, tuple[str, str]] = {}
    for attr_name, gen in entity_ir.attributes:
        gctx = GenContext(
            siblings=attributes, pools=pools, ref_keys=ref_keys, expr_resolver=None,
        )
        gctx.siblings.setdefault("__now__", now_iso)
        value = gen(value_cursor, gctx)
        bijection = one_to_one.get(attr_name)
        if bijection is not None:
            _rel, target_entity = bijection
            target_keys = pools.live_keys(target_entity)
            if ordinal < len(target_keys):
                value = target_keys[ordinal]
        attributes[attr_name] = value
        rel = getattr(gen, "__df_relationship__", None)
        if rel is not None and isinstance(value, str):
            ref_keys[attr_name] = (pools.relationship_target(rel), value)
    attributes.pop("__now__", None)
    attributes.pop("__virtual_epoch_ms__", None)
    attributes[entity_ir.key_attribute] = key

    pools.insert(PooledEntity(
        entity_key=key, entity_type=entity_ir.name, attributes=attributes,
        entity_version=1, created_at=now_iso, updated_at=now_iso,
    ))


def _emit_snapshots(
    ir: ManifestIR, entity_ir: EntityIR, pools: EntityPools, tree: SeedTree,
    identity: StreamIdentity, sequence: SequenceCounter,
    *, occurred_at: datetime, emitted_at: datetime,
    shard_id: int = 0, shard_count: int = 1,
) -> list[InternalEnvelope]:
    out: list[InternalEnvelope] = []
    snap_cursor = Cursor(tree.key("values", f"snapshot:{entity_ir.name}"))
    bits = UuidBits(snap_cursor)
    for key in pools.live_keys(entity_ir.name):
        # Each seeded entity's head snapshot is emitted by exactly one shard — its
        # owner under the stable hash partition (no cross-shard snapshot duplication).
        # The shared id-minting cursor (``snap_cursor``) advances exactly one 74-bit
        # draw per live key REGARDLESS of which shard emits it, so a key's UUIDv7
        # event_id is identical on every shard (replay-stable, §7.2). A skipped key
        # therefore still burns its one draw — the per-key id is deterministic and the
        # owning shard mints the same id it would in the single-shard layout.
        if not owns_key(key, shard_id=shard_id, shard_count=shard_count):
            bits.next_random_74()  # keep cursor parity: one draw per live key
            continue
        record = pools.require(entity_ir.name, key)
        tx = PoolTransaction(ir, identity, occurred_at=occurred_at, emitted_at=emitted_at)
        event_id = event_id_for(occurred_at, bits)
        tx.record_mutation(Mutation(
            entity_type=entity_ir.name, entity_key=key, op="r",
            before=None, after=record.row_image(), entity_version=1, event_id=event_id,
        ))
        out.extend(tx.commit(sequence))
    return out
