"""Determinism + structural-integrity tests over the synthetic manifest.

A tiny manifest produces a deterministic event sequence: same seed → byte-identical
under canonical serialization (the GOLD-A property in miniature), and different
seeds diverge. Also asserts the structural invariants that hold by construction:
gapless ``sequence_no``, monotone ``occurred_at``, payment-requires-order, and CDC
``c``/``r`` before any ``u`` (R-CDC-4).
"""

from __future__ import annotations

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.envelope import canonical_serialize

from .fixtures import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WORKSPACE_ID,
    FixedWallClock,
    synthetic_manifest,
)


def _run(seed: int, max_events: int = 400) -> list[bytes]:
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=seed, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    shard = Shard(ir, config, FixedWallClock())
    return [canonical_serialize(e) for e in shard.run_batch(max_events=max_events)]


def _run_raw(seed: int, max_events: int = 400):  # type: ignore[no-untyped-def]
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=seed, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    shard = Shard(ir, config, FixedWallClock())
    return shard.run_batch(max_events=max_events)


def test_same_seed_is_byte_identical() -> None:
    """GOLD-A in miniature: same seed → byte-identical canonical sequence."""
    a = _run(42)
    b = _run(42)
    assert a and a == b


def test_different_seed_diverges() -> None:
    a = _run(42)
    c = _run(43)
    assert a != c


def test_first_divergence_is_reportable() -> None:
    """On mismatch the first divergent line is identifiable (GOLD-A reporting)."""
    a = _run(42)
    c = _run(43)
    diverge = next((i for i, (x, y) in enumerate(zip(a, c, strict=False)) if x != y), None)
    assert diverge is not None


def test_sequence_no_is_gapless_and_monotone() -> None:
    rows = _run_raw(42)
    seqs = [e["sequence_no"] for e in rows]
    assert seqs == list(range(1, len(seqs) + 1))


def test_occurred_at_non_decreasing() -> None:
    rows = _run_raw(42)
    occ = [e["occurred_at"] for e in rows]
    assert occ == sorted(occ)


def test_payment_requires_a_prior_order() -> None:
    """Structural: every payment_authorized has a prior order_placed on the same
    order (INV-GEN-2; no payment without order)."""
    rows = _run_raw(42)
    placed_orders: set[str] = set()
    for e in rows:
        if e["event_type"] == "order_placed":
            placed_orders.add(str(e["payload"]["order_id"]))
        elif e["event_type"] == "payment_authorized":
            assert str(e["payload"]["order_id"]) in placed_orders


def test_cdc_create_or_read_precedes_update() -> None:
    """R-CDC-4: no ``u`` before the entity's ``c``/``r`` within the stream."""
    rows = _run_raw(42)
    seen_create: set[tuple[str, str]] = set()
    for e in rows:
        op = e["op"]
        if op is None:
            continue
        ref = e["entity_refs"][0]
        ident = (ref["entity_type"], ref["entity_key"])
        if op in ("c", "r"):
            seen_create.add(ident)
        elif op in ("u", "d"):
            assert ident in seen_create, f"{op} before c/r for {ident}"


def test_twenty_key_envelope_and_canonical_df() -> None:
    rows = _run_raw(42)
    sample = rows[0]
    assert "_df" in sample
    assert sample["_df"]["canonical"] is True
    # 20 delivered fields + _df.
    assert len([k for k in sample if k != "_df"]) == 20
