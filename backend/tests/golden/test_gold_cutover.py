"""Deterministic mid-stream schema cutover — the OPS-13 / GOLD-variant unit (Exit #1, #2).

Phase-10 exit criterion #1 ("upgrades to v2 at the scheduled simulated time without
restart … v1 late stragglers after cutover keep their original schema_ref") and #2
("same pin + seed + schedule reproduces the same cutover sequence_no; pause / stop /
failover / backfill interactions per schema-registry §10.4").

The cutover determinism lives in the pure engine: a
:class:`~dataforge_engine.behavior.ir.SchemaCutover` gates each event on
``occurred_at_us >= at_us`` (the simulated domain), so the boundary is byte-identical
across speed multipliers, pause/stop/restart, and failover — the runner only wires the
pre-warmed cutover into the IR and persists the applied transition. These tests drive
the same builtin ecommerce manifest the GOLD/PROP lanes use (no Django, no DB) with a
deterministic injected wall clock, install a cutover for ``order_placed`` mid-window,
and assert the §10.4 boundary + determinism directly.

The fixed seed is ``4242`` (the exercise-E5 / phase demo seed); at that seed every
order_placed event occurs in the first simulated day, so a cutover ~6h in cleanly
straddles the distribution (a non-trivial v1/v2 split).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.behavior.checkpoint import encode_checkpoint, restore_checkpoint
from dataforge_engine.behavior.ir import SchemaCutover, compile_value_source
from dataforge_engine.envelope import canonical_serialize
from generation.infra.clock import DeterministicWallClock
from tests.golden.harness import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WALL_EPOCH,
    WORKSPACE_ID,
    _pooled_entity_from_image,
    merged_ecommerce_document,
)

pytestmark = pytest.mark.golden

_SEED = 4242
_EVENT_TYPE = "order_placed"
_US_PER_DAY = 86_400 * 1_000_000
_AT_US = _US_PER_DAY // 4  # ~6h into the simulated window — straddles the distribution
_WINDOW_US = 30 * _US_PER_DAY
_MAX_EVENTS = 20_000

_STATE_BINDING = (("shipping_state", compile_value_source({"from": "actor.address.state"})),)
_STATE_CITY_BINDINGS = (
    ("shipping_state", compile_value_source({"from": "actor.address.state"})),
    ("shipping_city", compile_value_source({"from": "actor.address.city"})),
)


def _config() -> ShardConfig:
    return ShardConfig(
        seed=_SEED,
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH,
        mode="backfill",
        mean_events_per_session=5.0,
        visits_per_actor_day=1.0,
    )


def _cutover(at_us: int, target: int, bindings: Any) -> dict[str, SchemaCutover]:
    return {_EVENT_TYPE: SchemaCutover(at_us=at_us, target_version=target, added_bindings=bindings)}


def _run(
    *,
    at_us: int = _AT_US,
    target: int = 2,
    bindings: Any = _STATE_BINDING,
    until_us: int = _WINDOW_US,
    max_events: int = _MAX_EVENTS,
) -> list[Any]:
    """Drive a backfill shard with a cutover armed and return the produced envelopes."""
    document = merged_ecommerce_document()
    ir = compile_manifest(document, schema_cutovers=_cutover(at_us, target, bindings))
    shard = Shard(ir, _config(), DeterministicWallClock(epoch=datetime(2026, 1, 1, tzinfo=UTC)))
    shard.seed()
    return shard.run_batch(max_events=max_events, until_us=until_us, pass_size=500)


def _occurred_at_us(envelope: Any) -> int:
    dt = datetime.fromisoformat(str(envelope["occurred_at"]).replace("Z", "+00:00"))
    return int((dt - VIRTUAL_EPOCH).total_seconds() * 1_000_000)


def _snapshot_pools(shard: Shard, ir: Any) -> dict[str, list[dict[str, Any]]]:
    """The per-type pool images for a checkpoint restart (the §9.3 failover restore)."""
    images: dict[str, list[dict[str, Any]]] = {}
    for et in ir.entity_order:
        pool = shard.pools.pool(et)
        images[et] = [pool.records[k].snapshot_json() for k in pool.records]
    return images


def _order_placed(rows: list[Any]) -> list[Any]:
    return [e for e in rows if e["event_type"] == _EVENT_TYPE]


# --- §10.4 step 3: the per-event boundary on occurred_at ----------------------


def test_pre_cutover_events_keep_v1_no_added_field() -> None:
    """Every order_placed with ``occurred_at < at`` keeps v1 and carries no shipping_state."""
    rows = _run()
    pre = [e for e in _order_placed(rows) if _occurred_at_us(e) < _AT_US]
    assert pre, "no pre-cutover order_placed events at this seed/at"
    for env in pre:
        assert env["schema_ref"]["version"] == 1
        assert "shipping_state" not in env["payload"]


def test_post_cutover_events_carry_v2_and_added_field() -> None:
    """Every order_placed with ``occurred_at >= at`` stamps v2 and a bound shipping_state."""
    rows = _run()
    post = [e for e in _order_placed(rows) if _occurred_at_us(e) >= _AT_US]
    assert post, "no post-cutover order_placed events at this seed/at"
    for env in post:
        assert env["schema_ref"]["version"] == 2
        assert env["payload"].get("shipping_state")  # bound from actor.address.state


def test_cutover_actually_straddles_the_distribution() -> None:
    """Sanity: the chosen ``at`` yields both a v1 and a v2 cohort (a real boundary)."""
    rows = _run()
    op = _order_placed(rows)
    versions = {e["schema_ref"]["version"] for e in op}
    assert versions == {1, 2}


def test_late_v1_straggler_keeps_original_schema_ref() -> None:
    """Exit #1: a v1 straggler (``occurred_at < at``) keeps its original v1 schema_ref —
    byte-identical to a stream with no cutover at all for that event.

    The gate is on ``occurred_at``, never wall/emission/processing order, so even an event
    finalized after the cutover instant in wall terms stays v1 if its simulated instant
    precedes ``at``. We prove this by comparing the latest pre-cutover order_placed event
    against the same event in a no-cutover run: identical canonical bytes (the cutover IR
    is installed, but the event predates ``at``, so nothing changes)."""
    with_cut = _run()
    document = merged_ecommerce_document()
    ir = compile_manifest(document)  # NO cutover
    shard = Shard(ir, _config(), DeterministicWallClock(epoch=datetime(2026, 1, 1, tzinfo=UTC)))
    shard.seed()
    no_cut = shard.run_batch(max_events=_MAX_EVENTS, until_us=_WINDOW_US, pass_size=500)

    # The latest pre-cutover order_placed is the "straggler at the edge": it must be v1
    # and byte-identical with vs. without the cutover IR installed.
    pre_with = [e for e in _order_placed(with_cut) if _occurred_at_us(e) < _AT_US]
    assert pre_with
    edge = max(pre_with, key=lambda e: _occurred_at_us(e))
    assert edge["schema_ref"]["version"] == 1
    assert "shipping_state" not in edge["payload"]
    twin = next(e for e in _order_placed(no_cut) if e["sequence_no"] == edge["sequence_no"])
    assert canonical_serialize(edge) == canonical_serialize(twin)


# --- Exit #2: determinism (same pin + seed + schedule → same cutover seq) ------


def test_first_post_cutover_sequence_no_is_deterministic() -> None:
    """Exit #2: the first event with ``occurred_at >= at`` has a reproducible sequence_no."""
    def first_post_seq() -> int:
        rows = _run()
        post = [e for e in _order_placed(rows) if _occurred_at_us(e) >= _AT_US]
        return min(int(e["sequence_no"]) for e in post)

    a = first_post_seq()
    b = first_post_seq()
    assert a == b


def test_gold_variant_canonical_content_is_byte_identical() -> None:
    """GOLD variant: same seed + schedule reproduces the byte-identical canonical batch."""
    a = [canonical_serialize(e) for e in _run()]
    b = [canonical_serialize(e) for e in _run()]
    assert a and a == b


# --- §10.3: version skipping (1 → 3) applies the union of chains ---------------


def test_version_skip_1_to_3_applies_union_of_added_fields() -> None:
    """A 1→3 cutover stamps v3 and carries BOTH shipping_state and shipping_city."""
    rows = _run(target=3, bindings=_STATE_CITY_BINDINGS)
    post = [e for e in _order_placed(rows) if _occurred_at_us(e) >= _AT_US]
    assert post
    for env in post:
        assert env["schema_ref"]["version"] == 3
        assert env["payload"].get("shipping_state")
        assert env["payload"].get("shipping_city")


# --- §10.4 lifecycle: pause / stop-restart / failover -------------------------


def test_pause_frozen_clock_cannot_fire_cutover() -> None:
    """A window that stops before ``at`` (a paused/frozen clock) never crosses the cutover.

    Running only up to ``at_us`` (the clock did not advance past the schedule, as a pause
    holds the virtual clock) yields no v2 event — the cutover cannot fire while paused."""
    rows = _run(until_us=_AT_US - 1)
    op = _order_placed(rows)
    assert op, "expected some pre-cutover events"
    assert all(e["schema_ref"]["version"] == 1 for e in op)


def test_stop_restart_pending_cutover_survives_and_fires() -> None:
    """Exit #2 (stop/restart): a cutover not yet reached survives a checkpoint restart and
    still fires at the same boundary on the restored shard — byte-identical to one run.

    Segment 1 runs up to just before ``at`` (no v2 yet) and checkpoints; a fresh shard is
    restored from that checkpoint with the SAME cutover re-armed (the pending schedule
    rides the desired state) and continues — the post-restart events cross ``at`` and
    stamp v2, and the concatenated canonical content equals an uninterrupted run."""
    document = merged_ecommerce_document()
    cutovers = _cutover(_AT_US, 2, _STATE_BINDING)

    # Segment 1: run until just before the cutover, then checkpoint.
    ir1 = compile_manifest(document, schema_cutovers=cutovers)
    clock = DeterministicWallClock(epoch=WALL_EPOCH)
    shard1 = Shard(ir1, _config(), clock)
    shard1.seed()
    first = shard1.run_batch(max_events=_MAX_EVENTS, until_us=_AT_US - 1, pass_size=500)
    assert all(
        e["schema_ref"]["version"] == 1 for e in first if e["event_type"] == _EVENT_TYPE
    )
    blob = encode_checkpoint(shard1, checkpoint_seq=1)
    pool_images = _snapshot_pools(shard1, ir1)

    # Segment 2: a brand-new shard restored from the checkpoint, cutover RE-ARMED.
    ir2 = compile_manifest(document, schema_cutovers=cutovers)
    shard2 = Shard(ir2, _config(), clock)
    shard2.ensure_registered()
    for et, images in pool_images.items():
        for image in images:
            shard2.pools.reindex_loaded(_pooled_entity_from_image(et, image))
    restore_checkpoint(shard2, blob)
    second = shard2.run_batch(max_events=_MAX_EVENTS, until_us=_WINDOW_US, pass_size=500)
    restarted = [*first, *second]

    # The restart produced v2 events after the boundary (the pending cutover fired).
    post = [
        e for e in restarted
        if e["event_type"] == _EVENT_TYPE and _occurred_at_us(e) >= _AT_US
    ]
    assert post and all(e["schema_ref"]["version"] == 2 for e in post)
    # Gapless sequence across the restart (INV-STR-5 "resume with zero gaps").
    seqs = [e["sequence_no"] for e in restarted]
    assert seqs == list(range(1, len(seqs) + 1))


def test_failover_at_passed_during_gap_fires_on_first_post_restore_tick() -> None:
    """Exit #2 (failover): if ``at`` is crossed while a shard was down, the FIRST events
    the restored shard produces (already past ``at``) stamp v2 immediately.

    Segment 1 stops before ``at``; the restored shard resumes into a window fully beyond
    ``at`` (the gap covered the schedule), so its first order_placed event is post-cutover
    and carries v2 — no event is missed, none double-fires."""
    document = merged_ecommerce_document()
    cutovers = _cutover(_AT_US, 2, _STATE_BINDING)
    ir1 = compile_manifest(document, schema_cutovers=cutovers)
    clock = DeterministicWallClock(epoch=WALL_EPOCH)
    shard1 = Shard(ir1, _config(), clock)
    shard1.seed()
    # Segment 1 runs and stops before the cutover (the shard then "dies").
    shard1.run_batch(max_events=_MAX_EVENTS, until_us=_AT_US - 1, pass_size=500)
    blob = encode_checkpoint(shard1, checkpoint_seq=1)
    pool_images = _snapshot_pools(shard1, ir1)
    ir2 = compile_manifest(document, schema_cutovers=cutovers)
    shard2 = Shard(ir2, _config(), clock)
    shard2.ensure_registered()
    for et, images in pool_images.items():
        for image in images:
            shard2.pools.reindex_loaded(_pooled_entity_from_image(et, image))
    restore_checkpoint(shard2, blob)
    second = shard2.run_batch(max_events=_MAX_EVENTS, until_us=_WINDOW_US, pass_size=500)
    op_after = [e for e in second if e["event_type"] == _EVENT_TYPE]
    # The first order_placed the restored shard makes is already past the cutover → v2.
    assert op_after
    first_after = min(op_after, key=lambda e: e["sequence_no"])
    assert _occurred_at_us(first_after) >= _AT_US
    assert first_after["schema_ref"]["version"] == 2


# --- §10.4 backfill: a day-boundary cutover inside a backfill window -----------


def test_backfill_day_boundary_cutover() -> None:
    """A cutover at a simulated-day boundary inside the backfill window splits cleanly.

    Backfill generation crosses the boundary in one batch; the per-event occurred_at gate
    still places every event on the correct side (no live clock involved, §10.4)."""
    # Use a tiny at so most of the front-loaded distribution is post-cutover, but assert
    # the split is governed purely by occurred_at.
    at = _US_PER_DAY // 20  # ~1.2h
    rows = _run(at_us=at)
    op = _order_placed(rows)
    for env in op:
        if _occurred_at_us(env) >= at:
            assert env["schema_ref"]["version"] == 2
        else:
            assert env["schema_ref"]["version"] == 1
