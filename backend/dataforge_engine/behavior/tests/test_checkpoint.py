"""Checkpoint codec round-trip + restore continuation (behavior-engine §9).

The codec blob is JSON-stable (encode → JSON → decode is identity), restoring
into a fresh shard (pools carried as a snapshot-image stand-in) continues the
canonical sequence byte-identically across the interruption (the determinism
boundary's pause/resume row), and a ``pin_echo`` mismatch is refused (T4).
"""

from __future__ import annotations

import json

import pytest

from dataforge_engine.behavior import (
    CheckpointError,
    Shard,
    ShardConfig,
    compile_manifest,
    encode_checkpoint,
    encode_to_json,
    restore_checkpoint,
)
from dataforge_engine.behavior.pools import PooledEntity
from dataforge_engine.envelope import canonical_serialize

from .fixtures import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WORKSPACE_ID,
    FixedWallClock,
    synthetic_manifest,
)

_END = 120_000_000  # 120 simulated seconds — a bounded full window.
_MID = 40_000_000


def _make(seed: int = 11) -> Shard:
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=seed, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    return Shard(ir, config, FixedWallClock())


def _copy_pools(src: Shard, dst: Shard) -> None:
    dst.ensure_registered()
    for name in src.ir.entity_order:
        pool = src.pools.pool(name)
        for key in pool.creation_order:
            rec = pool.records.get(key)
            if rec is not None:
                dst.pools.reindex_loaded(PooledEntity(
                    entity_key=rec.entity_key, entity_type=rec.entity_type,
                    attributes=dict(rec.attributes), entity_version=rec.entity_version,
                    created_at=rec.created_at, updated_at=rec.updated_at,
                    status=rec.status, in_session=rec.in_session,
                ))


def test_blob_is_json_stable() -> None:
    shard = _make()
    shard.run_batch(until_us=_MID)
    blob = encode_checkpoint(shard, checkpoint_seq=1)
    assert json.loads(json.dumps(blob)) == blob
    assert isinstance(encode_to_json(shard, checkpoint_seq=1), str)


def test_codec_fields_present() -> None:
    shard = _make()
    shard.run_batch(until_us=_MID)
    blob = encode_checkpoint(shard, checkpoint_seq=3)
    for key in ("codec_version", "pin_echo", "sequence_no_last", "vclock", "arrival",
                "timer_seq_next", "timers", "sessions", "lifecycles", "pool_counters"):
        assert key in blob
    assert blob["codec_version"] == 1
    assert blob["pin_echo"]["seed"] == 11


def test_restore_continuation_is_byte_identical() -> None:
    """Interrupt + checkpoint + restore continues byte-identically (§9.3)."""
    reference = _make()
    ref_rows = [canonical_serialize(e) for e in reference.run_batch(until_us=_END)]

    interrupted = _make()
    first = [canonical_serialize(e) for e in interrupted.run_batch(until_us=_MID)]
    blob = json.loads(json.dumps(encode_checkpoint(interrupted, checkpoint_seq=1)))

    resumed = _make()
    _copy_pools(interrupted, resumed)
    restore_checkpoint(resumed, blob)
    rest = [canonical_serialize(e) for e in resumed.run_batch(until_us=_END)]

    assert ref_rows == first + rest


def test_restore_rebases_arrival_cursor_to_next_index() -> None:
    """Restore re-anchors the arrival gap-draw cursor to ``next_index`` (§9.3).

    The arrival cursor consumes exactly one draw per arrival, so its position is the
    derivable invariant ``cursor.position == arrival.next_index``. The checkpoint blob
    records ``next_index`` but not the cursor position; a fresh restored shard builds
    the cursor at position 0, so restore MUST rebase it — otherwise the next
    inter-arrival gap is drawn at the wrong RNG position and the arrival schedule
    (hence every downstream session) silently diverges. This pins the rebase so the
    GOLD-D continuation defect (a restart producing a different arrival ordering than
    an uninterrupted run) cannot regress. Drives some arrivals, checkpoints, restores
    into a fresh shard, and asserts the cursor position equals the restored index."""
    shard = _make()
    shard.run_batch(until_us=_MID)
    # The invariant must already hold on the live shard.
    live_pos = shard.arrival._cursor.position
    advanced_index = shard.arrival.state.next_index
    assert live_pos == advanced_index
    assert advanced_index > 0  # arrivals actually fired (the test is not vacuous)

    blob = json.loads(json.dumps(encode_checkpoint(shard, checkpoint_seq=1)))
    resumed = _make()
    _copy_pools(shard, resumed)
    assert resumed.arrival._cursor.position == 0  # fresh cursor before restore
    restore_checkpoint(resumed, blob)
    # After restore the cursor is rebased to the restored index — not left at 0.
    assert resumed.arrival.state.next_index == advanced_index
    assert resumed.arrival._cursor.position == advanced_index


def test_pin_echo_mismatch_refused() -> None:
    shard = _make(seed=11)
    shard.run_batch(until_us=_MID)
    blob = encode_checkpoint(shard, checkpoint_seq=1)
    other = _make(seed=99)  # different seed ⇒ pin mismatch
    _copy_pools(shard, other)
    with pytest.raises(CheckpointError):
        restore_checkpoint(other, blob)


def test_unknown_codec_version_refused() -> None:
    shard = _make()
    shard.run_batch(until_us=_MID)
    blob = encode_checkpoint(shard, checkpoint_seq=1)
    blob["codec_version"] = 99
    with pytest.raises(CheckpointError):
        restore_checkpoint(_make(), blob)
