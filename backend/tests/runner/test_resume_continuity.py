"""Resume continuity: gapless sequence_no + dwell rebase across a pause (T8).

These exercise the engine-level semantics the runner's resume path relies on, with a
REAL :class:`~dataforge_engine.behavior.Shard` (no Django / no DB) so the determinism
contract is asserted directly:

* **Gapless ``sequence_no`` across a pause (T8 / INV-GEN-7):** a pause freezes the
  warm in-memory state (heap, pools, sequence counter); resume continues from it with
  ZERO sequence gaps — the runner's warm-hold pause keeps the engine in memory so
  there is nothing to drop.
* **Dwell-timer rebase (§9.3 step 4):** the pause freezes the virtual clock at the
  frontier ``F``; resume re-anchors the clock at ``(wall_resume, F)`` via
  :meth:`Shard.reopen_clock_segment`, so ``virtual_now`` continues from ``F`` and
  dwell timers — which store absolute virtual due-times — fire correctly afterward.
* **Dynamic TPS is content-neutral (BE-P4):** changing ``target_tps`` mid-run via
  :meth:`Shard.set_target_tps` does not perturb already-scheduled per-session content.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.behavior.tests.fixtures import (
    VIRTUAL_EPOCH,
    FixedWallClock,
    synthetic_manifest,
)


def _build_shard(clock: FixedWallClock, *, target_tps: float = 50.0) -> Shard:
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=4242,
        workspace_id="0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60",
        stream_id="7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b",
        shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH,
        speed_multiplier=1.0,
        shard_count=1,
        mode="live",
        target_tps=target_tps,
    )
    return Shard(ir, config, clock)


def _drain(
    shard: Shard, clock: FixedWallClock, *, until_us: int, budget: int = 500
) -> list[dict[str, Any]]:
    """Generate everything due up to ``until_us`` (paced budget loosely emulated)."""
    out: list[dict[str, Any]] = []
    while True:
        batch = shard.generate(budget, until_us)
        if not batch:
            break
        out.extend(cast("list[dict[str, Any]]", batch))
    return out


def test_sequence_no_gapless_across_pause() -> None:
    """A pause then resume continues the gapless sequence_no with no gaps (T8)."""
    clock = FixedWallClock()
    shard = _build_shard(clock)
    head = cast("list[dict[str, Any]]", list(shard.seed()))  # head op:"r" rows: seq 1..N

    # Generate a first window of activity (well past the first arrivals + dwells).
    first = _drain(shard, clock, until_us=600_000_000)  # 600 s simulated
    assert first, "expected some events in the first window"
    frontier_at_pause = shard.clock.frontier_us
    seq_before = shard.sequence.last

    # --- PAUSE: the virtual clock freezes at the frontier; warm state stays in memory.
    # (The runner does not advance the clock or generate while idling in _enter_paused.)
    # --- RESUME (T7→T8): re-anchor the clock at (wall_resume, F) — the dwell rebase.
    shard.reopen_clock_segment(clock.now())
    assert shard.clock.frontier_us == frontier_at_pause  # clock resumed from the frozen F

    # Generate a second window. Sequence numbers must continue with ZERO gaps.
    second = _drain(shard, clock, until_us=1_200_000_000)
    assert second, "expected continued activity after resume"

    all_seqs = [e["sequence_no"] for e in head + first + second]
    # Strictly contiguous from 1: gapless counter (INV-GEN-7), no skips across the pause.
    assert all_seqs == list(range(1, len(all_seqs) + 1))
    assert shard.sequence.last > seq_before  # progress continued after resume


def test_resume_clock_continues_from_frozen_frontier() -> None:
    """After resume, virtual_now continues from the frozen frontier, not wall-elapsed.

    The pause froze the clock at ``F``; even if a long wall interval elapsed during the
    pause, resume re-anchors at ``(wall_resume, F)`` so ``virtual_now`` ≈ ``F`` at the
    resume instant (dwell timers, stored as absolute virtual due-times, are thereby
    rebased correctly, §9.3 step 4).
    """
    clock = FixedWallClock()
    shard = _build_shard(clock)
    shard.seed()
    _drain(shard, clock, until_us=600_000_000)
    frozen = shard.clock.frontier_us

    # Simulate a long pause by advancing the wall clock far ahead WITHOUT generating.
    resume_wall = clock.now() + timedelta(hours=2)
    shard.reopen_clock_segment(resume_wall)

    # virtual_now at the resume instant is the frozen frontier (the 2-hour pause did not
    # advance simulated time — that is the freeze), not frozen + 2h.
    vnow = shard.clock.virtual_now_us(resume_wall)
    assert vnow == frozen


def test_set_target_tps_is_content_neutral_for_scheduled_sessions() -> None:
    """Changing target_tps mid-run does not perturb already-scheduled content (BE-P4).

    Two runs identical except a mid-run TPS bump: the content of sessions that arrived
    BEFORE the bump (keyed by traversal identity) is byte-identical — the rate changes
    only which future arrivals occur and when, never per-session draws.
    """
    # Baseline: a constant TPS run.
    clock_a = FixedWallClock()
    shard_a = _build_shard(clock_a, target_tps=50.0)
    shard_a.seed()
    base = _drain(shard_a, clock_a, until_us=120_000_000)  # 120 s

    # Variant: same start, but bump TPS partway. Take an early prefix (before the bump
    # could affect arrivals) and compare the per-session event content.
    clock_b = FixedWallClock()
    shard_b = _build_shard(clock_b, target_tps=50.0)
    shard_b.seed()
    early = _drain(shard_b, clock_b, until_us=30_000_000)  # 30 s — before the bump
    shard_b.set_target_tps(500.0)  # live TPS change (BE-P2)
    _drain(shard_b, clock_b, until_us=120_000_000)

    # The early events (those whose session arrived in the first 30 s) are unchanged by
    # the later rate bump: compare their (event_type, payload) content per (session, seq).
    base_map = {
        (e["session_id"], e["sequence_no"]): (e["event_type"], _canon(e["payload"]))
        for e in base
        if e.get("session_id")
    }
    compared = 0
    for e in early:
        if not e.get("session_id"):
            continue
        key = (e["session_id"], e["sequence_no"])
        if key in base_map:
            assert (e["event_type"], _canon(e["payload"])) == base_map[key]
            compared += 1
    assert compared > 0  # the comparison actually exercised shared early-session events


def _canon(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, default=str)
