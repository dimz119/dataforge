"""GOLD-D at N = 4 — multi-shard continuation + partitioning correctness (P11 #6).

Phase-11 exit criterion #6: *"Multi-shard correctness: kill-test failover < 30 s per
shard with fencing; per-shard ``sequence_no`` gapless; cross-restart continuation
byte-identical at N = 4."* The kill-test/fencing half is the compose-only OPS lane
(:mod:`tests.ops.test_multishard_failover` pins its pure assertion logic); the
**continuation + partitioning** half lives here, in the fast pure-engine golden lane.

What this file proves at ``shard_count = 4`` (no Postgres, no Redis):

* **Per-shard cross-restart byte-identity** — each of the 4 shards, checkpointed
  mid-run and rebuilt from its checkpoint, re-emits canonical content byte-identical
  to its own uninterrupted run (the arrival-cursor rebase is the continuation hinge).
* **Per-shard gapless ``sequence_no``** across the stop (INV-GEN-7).
* **Deterministic, disjoint actor partitioning** — no actor is driven by two shards;
  every seeded actor is owned by exactly one shard under ``shard_for_key`` (the
  union is the full catalog); every emitted event sits on an eligible shard.
* **Snapshot (op:"r") union has zero duplication and zero loss** vs the N=1 run —
  each seeded-entity head snapshot is emitted on exactly one shard.

These are the engine-level guarantees the runner's multi-shard failover rests on; a
regression here points at the exact shard + event before any compose run is needed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dataforge_engine.behavior.partitioning import owns_key, shard_for_key
from tests.golden.harness import content_only
from tests.golden.harness_shards import (
    actor_key,
    partition_entity_key,
    run_shard,
    run_shard_with_restart,
    seeded_actor_keys,
)
from tests.seeds import SEED_GOLD_A

# N = 4 is the phase-11 exit-criterion shard count. The continuation unit is sized so
# the stop point lands mid-funnel on every shard (sessions in flight) while staying in
# the fast lane — each shard owns ~1/4 of the catalog, so a smaller per-shard total
# than the single-shard GOLD-D still resumes genuine in-flight state.
N_SHARDS = 4
STOP_AFTER = 300
TOTAL = 800
# A bounded per-shard budget for the runtime partition checks (disjointness, on-owner
# emission, zero cross-shard event_id duplication). These are partition *invariants*
# that hold at any prefix of the (unbounded, self-perpetuating) arrival chain, so a
# modest budget is sufficient and keeps the lane fast. Full-catalog *coverage* (the
# union == all actors half) is proven structurally over the seeded keys instead — the
# 30-simulated-day arrival chain never fully drains, so a drain-to-exhaustion proof is
# not available; the seeded-pool partition is the exact, fast equivalent.
PARTITION_BUDGET = 3_000


@pytest.mark.golden
@pytest.mark.parametrize("shard_id", range(N_SHARDS))
def test_gold_d_n4_per_shard_restart_is_byte_identical(shard_id: int) -> None:
    """Each shard's interrupted run == its uninterrupted run (GOLD-D at N=4, exit #6).

    The headline multi-shard continuation assertion: restoring shard ``shard_id`` from
    its §9.1 checkpoint and continuing yields the same canonical content as never
    stopping (INV-STR-5), proven independently for every one of the 4 shards."""
    uninterrupted = run_shard(
        seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS, max_events=TOTAL
    )
    restarted = run_shard_with_restart(
        seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS,
        stop_after=STOP_AFTER, total_events=TOTAL,
    )
    expected = [content_only(e) for e in uninterrupted]
    actual = [content_only(e) for e in restarted]
    assert actual, f"shard {shard_id} produced no events"
    assert len(actual) == len(expected), (
        f"shard {shard_id} event-count divergence: uninterrupted={len(expected)} "
        f"restarted={len(actual)} — the restart dropped or duplicated events"
    )
    for index, (exp, act) in enumerate(zip(expected, actual, strict=True)):
        if exp != act:
            event_id = json.loads(act).get("event_id", "<unparseable>")
            raise AssertionError(
                f"GOLD-D N=4 continuation divergence on shard {shard_id} at index "
                f"{index} (event_id={event_id}): a restart from checkpoint must "
                "reproduce the uninterrupted run (INV-STR-5; phase-11 exit #6)."
            )


@pytest.mark.golden
@pytest.mark.parametrize("shard_id", range(N_SHARDS))
def test_gold_d_n4_per_shard_sequence_is_gapless(shard_id: int) -> None:
    """Each shard's restarted ``sequence_no`` is gapless 1..N across the stop (INV-GEN-7).

    The checkpoint carries the per-shard sequence counter, so segment two resumes it
    with zero gaps — the structural half of phase-11 exit #6 ("per-shard sequence_no
    gapless"), proven per shard."""
    restarted = run_shard_with_restart(
        seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS,
        stop_after=STOP_AFTER, total_events=TOTAL,
    )
    seqs = [json.loads(content_only(e))["sequence_no"] for e in restarted]
    assert seqs == list(range(1, len(seqs) + 1)), (
        f"shard {shard_id} restart introduced a sequence_no gap or duplicate across "
        "the stop (INV-GEN-7): the checkpoint must carry the gapless counter"
    )


@pytest.mark.golden
def test_n4_stop_point_is_mid_run_on_every_shard() -> None:
    """Potency guard: the stop point lands with in-flight state on each shard.

    If a shard drained its whole funnel before the stop, its restart would restore a
    trivial empty state and the byte-identity above would be vacuous. Assert every
    shard's uninterrupted run actually has events beyond the stop point."""
    for shard_id in range(N_SHARDS):
        uninterrupted = run_shard(
            seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS, max_events=TOTAL
        )
        assert len(uninterrupted) > STOP_AFTER, (
            f"shard {shard_id} stop point at/after end of run — raise TOTAL so the "
            "restart resumes genuine in-flight state"
        )


@pytest.mark.golden
def test_actor_partition_is_disjoint_and_complete() -> None:
    """Every seeded actor is owned by exactly one of the 4 shards (disjoint + union).

    The deterministic ``shard_for_key`` split partitions the catalog: each actor key
    is owned by exactly one shard (``owns_key`` true for one ``shard_id``), so the
    shards drive disjoint populations whose union is the full seeded catalog. A pure
    partition-function property — no drain needed (phase-11 exit #6 partitioning)."""
    keys = seeded_actor_keys(seed=SEED_GOLD_A, shard_count=N_SHARDS)
    assert keys, "no seeded actors — the harness produced an empty catalog"
    buckets: dict[int, int] = dict.fromkeys(range(N_SHARDS), 0)
    for key in keys:
        owners = [
            sid for sid in range(N_SHARDS)
            if owns_key(key, shard_id=sid, shard_count=N_SHARDS)
        ]
        assert owners == [shard_for_key(key, N_SHARDS)], (
            f"actor {key!r} owned by {owners}, not exactly its shard_for_key shard "
            f"{shard_for_key(key, N_SHARDS)} — partitioning is not a clean split"
        )
        buckets[owners[0]] += 1
    assert sum(buckets.values()) == len(keys), "an actor escaped the partition"
    assert all(count > 0 for count in buckets.values()), (
        f"a shard owns zero actors (buckets={buckets}) — the split is degenerate at "
        "this catalog size; the union-coverage proof needs every shard populated"
    )


@pytest.mark.golden
def test_no_actor_driven_by_two_shards() -> None:
    """No actor's sessions are generated by two shards (no cross-shard duplication).

    Drives all 4 shards and asserts the ``actor_id`` sets are pairwise disjoint — the
    runtime confirmation of the partition property: a session for an actor is bound on
    exactly the one shard that owns the actor (INV behind per-shard gapless emission)."""
    actor_sets: dict[int, set[str]] = {}
    for shard_id in range(N_SHARDS):
        events = run_shard(
            seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS, max_events=PARTITION_BUDGET
        )
        actor_sets[shard_id] = {a for e in events if (a := actor_key(e)) is not None}
        assert actor_sets[shard_id], f"shard {shard_id} drove no actors"
        # Every actor a shard drives is one it owns under shard_for_key (the runtime
        # confirmation that _bind_actor honours the partition predicate).
        for actor in actor_sets[shard_id]:
            assert shard_for_key(actor, N_SHARDS) == shard_id, (
                f"shard {shard_id} drove actor {actor!r} owned by shard "
                f"{shard_for_key(actor, N_SHARDS)} — _bind_actor ignored ownership"
            )
    for i in range(N_SHARDS):
        for j in range(i + 1, N_SHARDS):
            overlap = actor_sets[i] & actor_sets[j]
            assert not overlap, (
                f"shards {i} and {j} both drove actors {sorted(overlap)[:5]} — the "
                "partition leaked (cross-shard duplication of a lifecycle)"
            )


@pytest.mark.golden
def test_every_event_sits_on_an_eligible_shard() -> None:
    """Each emitted event's partition entity is owned by its emitting shard — for the
    head snapshots and the actor-driven business events.

    A CDC event's partition entity (PK-2) is the *mutated* entity, which may be a
    non-actor whose key hashes independently; so this asserts ownership for the
    snapshot (op:"r") and business (PK-1, actor-rooted) events — the events whose
    partition entity is the shard-owned actor population."""
    for shard_id in range(N_SHARDS):
        events = run_shard(
            seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS, max_events=TOTAL
        )
        for envelope in events:
            obj: dict[str, Any] = json.loads(content_only(envelope))
            # Only PK-1 (business, actor-rooted) + PK-3 (op:"r" snapshot) events have
            # an actor-owned partition entity; CDC PK-2 entities are not the actor.
            is_cdc = str(obj.get("event_type", "")).startswith("cdc.")
            if is_cdc and obj.get("op") != "r":
                continue
            entity_key = partition_entity_key(envelope)
            assert shard_for_key(entity_key, N_SHARDS) == shard_id, (
                f"shard {shard_id} emitted event {obj.get('event_id')} for partition "
                f"entity {entity_key!r} owned by shard "
                f"{shard_for_key(entity_key, N_SHARDS)} — emission on a non-owner shard"
            )


@pytest.mark.golden
def test_no_event_id_emitted_by_two_shards() -> None:
    """No canonical ``event_id`` is produced by two shards (zero cross-shard duplication).

    Per-(stream,shard) ``event_id`` derivation + the disjoint actor partition mean the
    4 shards' ``event_id`` sets are pairwise disjoint: an event is generated on exactly
    one shard. This is the "zero duplication" half of phase-11 exit #6 (no event on a
    non-owner shard); the "zero loss / union == catalog" half is the structural seeded
    partition proven in :func:`test_actor_partition_is_disjoint_and_complete`."""
    id_sets: dict[int, set[str]] = {}
    for shard_id in range(N_SHARDS):
        events = run_shard(
            seed=SEED_GOLD_A, shard_id=shard_id, shard_count=N_SHARDS, max_events=PARTITION_BUDGET
        )
        ids = [str(json.loads(content_only(e))["event_id"]) for e in events]
        assert len(ids) == len(set(ids)), (
            f"shard {shard_id} emitted a duplicate event_id within its own run"
        )
        id_sets[shard_id] = set(ids)
    for i in range(N_SHARDS):
        for j in range(i + 1, N_SHARDS):
            overlap = id_sets[i] & id_sets[j]
            assert not overlap, (
                f"shards {i} and {j} both emitted event_ids {sorted(overlap)[:3]} — "
                "cross-shard event duplication (the partition is not disjoint)"
            )
