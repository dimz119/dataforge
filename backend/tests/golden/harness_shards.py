"""Multi-shard generation harness for the GOLD-D N=4 continuation gate (Phase 11).

Phase 11 generalizes a stream to ``shard_count = N`` shard workers. Each shard is an
independent :class:`~dataforge_engine.behavior.Shard` at the *same* seed, virtual
epoch, and deterministic wall clock; every shard seeds the **full** catalog, but a
shard only **binds sessions** for the actors it owns under ``shard_for_key`` so the
shards drive a disjoint actor population (no cross-shard duplication of a lifecycle)
with each shard's own gapless ``sequence_no`` (INV-GEN-7).

This module is the multi-shard analogue of :mod:`tests.golden.harness`:

* :func:`run_shard` drives one shard of an ``N``-way split to ``max_events``;
* :func:`run_shard_with_restart` runs one shard to a stop point, checkpoints +
  snapshots its pools, restarts a fresh :class:`Shard` from that checkpoint, and
  continues — the exact §9.3 failover/resume restore path, per shard, so GOLD-D
  can assert cross-restart byte-identity **at N = 4** (phase-11 exit #6).

Pure engine + ports (no Postgres, no Redis) — the fast golden lane, identical in
spirit to the single-shard harness it reuses constants and helpers from.
"""

from __future__ import annotations

import json
from typing import Any

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.behavior.checkpoint import encode_checkpoint, restore_checkpoint
from dataforge_engine.envelope import canonical_serialize
from generation.infra.clock import DeterministicWallClock
from tests.golden.harness import (
    SIMULATED_DAYS,
    STREAM_ID,
    VIRTUAL_EPOCH,
    WALL_EPOCH,
    WORKSPACE_ID,
    _pooled_entity_from_image,
    merged_ecommerce_document,
)

_US_PER_DAY = 86_400 * 1_000_000
_MEAN_EVENTS_PER_SESSION = 5.0
_VISITS_PER_ACTOR_DAY = 1.0


def _config(*, seed: int, shard_id: int, shard_count: int) -> ShardConfig:
    """A backfill :class:`ShardConfig` for one shard of an ``shard_count``-way split."""
    return ShardConfig(
        seed=seed,
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        shard_id=shard_id,
        virtual_epoch=VIRTUAL_EPOCH,
        mode="backfill",
        mean_events_per_session=_MEAN_EVENTS_PER_SESSION,
        visits_per_actor_day=_VISITS_PER_ACTOR_DAY,
        shard_count=shard_count,
    )


def seeded_actor_keys(*, seed: int, shard_count: int) -> list[str]:
    """Every seeded actor key (the full catalog actor pool, identical on each shard).

    Each shard seeds the same complete catalog, so this is the population the
    ``shard_for_key`` partition splits across the ``shard_count`` shards.
    """
    ir = compile_manifest(merged_ecommerce_document(None))
    shard = Shard(
        ir, _config(seed=seed, shard_id=0, shard_count=shard_count),
        DeterministicWallClock(epoch=WALL_EPOCH),
    )
    shard.seed()
    return list(shard.pools.pool(ir.actor_entity).records)


def run_shard(
    *, seed: int, shard_id: int, shard_count: int, max_events: int,
    simulated_days: int = SIMULATED_DAYS, pass_size: int = 500,
) -> list[Any]:
    """Drive one shard of an ``shard_count``-way split to ``max_events``.

    Returns the produced canonical envelope list (the head ``op:"r"`` snapshot batch
    for the keys this shard owns, then the live/CDC events for its owned actors).
    """
    ir = compile_manifest(merged_ecommerce_document(None))
    shard = Shard(
        ir, _config(seed=seed, shard_id=shard_id, shard_count=shard_count),
        DeterministicWallClock(epoch=WALL_EPOCH),
    )
    head = shard.seed()
    rest = shard.run_batch(
        max_events=max_events, until_us=simulated_days * _US_PER_DAY, pass_size=pass_size
    )
    return [*head, *rest]


def run_shard_with_restart(
    *, seed: int, shard_id: int, shard_count: int, stop_after: int, total_events: int,
    simulated_days: int = SIMULATED_DAYS, pass_size: int = 500,
) -> list[Any]:
    """One shard run to ``stop_after``, checkpointed, restarted, continued to total.

    Mirrors :func:`tests.golden.harness.build_batch_with_restart` exactly — encode
    the §9.1 checkpoint, snapshot the per-type pool images, build a NEW shard at the
    same pin, reload the pool images, ``restore_checkpoint``, and continue — but for
    one shard of an ``shard_count``-way split. The wall clock is carried across the
    restart so the restored segment keeps stamping monotonically.
    """
    document = merged_ecommerce_document(None)
    ir = compile_manifest(document)
    clock = DeterministicWallClock(epoch=WALL_EPOCH)
    shard = Shard(ir, _config(seed=seed, shard_id=shard_id, shard_count=shard_count), clock)
    head = shard.seed()
    first = shard.run_batch(
        max_events=stop_after, until_us=simulated_days * _US_PER_DAY, pass_size=pass_size
    )
    segment_one = [*head, *first]

    blob = encode_checkpoint(shard, checkpoint_seq=1)
    pool_images: dict[str, list[dict[str, Any]]] = {}
    for entity_type in ir.entity_order:
        pool = shard.pools.pool(entity_type)
        pool_images[entity_type] = [pool.records[k].snapshot_json() for k in pool.records]

    ir2 = compile_manifest(document)
    shard2 = Shard(ir2, _config(seed=seed, shard_id=shard_id, shard_count=shard_count), clock)
    shard2.ensure_registered()
    for entity_type, images in pool_images.items():
        for image in images:
            shard2.pools.reindex_loaded(_pooled_entity_from_image(entity_type, image))
    restore_checkpoint(shard2, blob)
    second = shard2.run_batch(
        max_events=total_events - len(segment_one),
        until_us=simulated_days * _US_PER_DAY,
        pass_size=pass_size,
    )
    return [*segment_one, *second]


def partition_entity_key(envelope: Any) -> str:
    """The PK-1..3 partition entity key — the 4th ``partition_key`` segment.

    ``partition_key = workspace_id:stream_id:partition_entity_type:partition_entity_key``
    (event-model §2.2.3). The 4th segment is the entity ``shard_for_key`` buckets.
    """
    obj: dict[str, Any] = json.loads(canonical_serialize(envelope))
    return str(obj["partition_key"]).split(":", 3)[3]


def actor_key(envelope: Any) -> str | None:
    """The driving actor's key for an envelope (``actor_id``), or ``None``.

    The actor population is what the shard split makes disjoint; a CDC event's
    *partition* entity may be a non-actor (e.g. an order), so actor-disjointness is
    asserted over ``actor_id`` rather than ``partition_key``.
    """
    obj: dict[str, Any] = json.loads(canonical_serialize(envelope))
    value = obj.get("actor_id")
    return str(value) if value else None
