"""Phase 8 realism: intensity curves, virtual-clock multiplier, backfill (P8-07..09).

Covers, against the synthetic manifest (zero scenario code — all curve/clock/mode
behavior is generic engine capability driven by manifest *data*):

* **Curves (§3.4)** — renormalization to mean 1.0 (the curve shape NEVER changes
  average TPS); local-hour evaluation; a curve reshapes *when* arrivals land while
  holding the daily mean exactly equal to the flat schedule.
* **Speed multiplier (§3)** — segment arithmetic compresses simulated time per
  wall second; pause freezes virtual time, resume rebases a fresh segment;
  checkpoint clock-position round-trips.
* **Backfill (§8)** — unpaced over [virtual_epoch, +N days], the head ``op:"r"``
  snapshot block leads the JSONL body; determinism holds with curves + multiplier.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from dataforge_engine.behavior import (
    Shard,
    ShardConfig,
    VirtualClock,
    compile_intensity,
    compile_manifest,
)
from dataforge_engine.behavior.clock import virtual_epoch_ms
from dataforge_engine.behavior.rng import Cursor
from dataforge_engine.behavior.scheduler import ArrivalProcess
from dataforge_engine.seeds import SeedTree

from .fixtures import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WORKSPACE_ID,
    FixedWallClock,
    synthetic_manifest,
)

_US_PER_DAY = 86_400 * 1_000_000


# --------------------------------------------------------------------------- #
# Curves — renormalization keeps the daily average exactly.                    #
# --------------------------------------------------------------------------- #


def test_diurnal_renormalizes_to_mean_one() -> None:
    """Any diurnal shape renormalizes so the 24-hour mean is exactly 1.0 (§3.4)."""
    curve = compile_intensity(
        {
            "diurnal": [
                {"from_hour": 0, "to_hour": 6, "multiplier": 0.2},
                {"from_hour": 6, "to_hour": 18, "multiplier": 1.5},
                {"from_hour": 18, "to_hour": 24, "multiplier": 3.0},
            ]
        }
    )
    assert abs(sum(curve.diurnal) / 24 - 1.0) < 1e-12
    assert not curve.is_flat


def test_weekly_renormalizes_to_mean_one() -> None:
    """Any weekly shape renormalizes so the 7-day mean is exactly 1.0 (§3.4)."""
    curve = compile_intensity(
        {"weekly": {"mon": 8, "tue": 8, "wed": 8, "thu": 8, "fri": 8, "sat": 2, "sun": 1}}
    )
    assert abs(sum(curve.weekly) / 7 - 1.0) < 1e-12


def test_no_intensity_section_is_flat() -> None:
    """A manifest with no `intensity` is flat 1.0 everywhere (no curve)."""
    curve = compile_intensity(None)
    assert curve.is_flat
    assert curve.at(0, 0) == 1.0
    assert curve.at(13 * 3_600_000_000, 0) == 1.0


def _count_arrivals(curve_section: dict[str, Any] | None, *, seed: int, days: int) -> int:
    """Drive the arrival inversion over `days` simulated days; return arrival count.

    Uses the same arrival cursor + rho the Shard uses, so the count is exactly the
    number of sessions the engine would spawn over the window.
    """
    curve = compile_intensity(curve_section, tz_name="UTC")
    tree = SeedTree(seed)
    arrival = ArrivalProcess(Cursor(tree.key("transitions", "arrival:0")))
    vem = virtual_epoch_ms(VIRTUAL_EPOCH)
    rho = 50.0 / 86_400.0  # 50 sessions/simulated-day base density (flat)
    window_end = days * _US_PER_DAY
    count = 0
    due = arrival.next_arrival_us_curved(rho, curve, vem)
    while due is not None and due <= window_end:
        count += 1
        due = arrival.next_arrival_us_curved(rho, curve, vem)
    return count


def test_curve_shape_never_changes_average_tps() -> None:
    """PROPERTY (binding §3.4): changing curve shape does not change the daily mean.

    A flat schedule and a strongly-peaked diurnal+weekly schedule, fed the same
    base rho over the same multi-day window, produce the same arrival *count* within
    Poisson sampling noise — because both curves average to 1.0. The renormalization
    is the mechanism: ``target_tps`` (the daily average) is preserved exactly.
    """
    days = 30
    peaked = {
        "diurnal": [
            {"from_hour": 0, "to_hour": 8, "multiplier": 0.1},
            {"from_hour": 8, "to_hour": 22, "multiplier": 2.5},
            {"from_hour": 22, "to_hour": 24, "multiplier": 0.3},
        ],
        "weekly": {
            "mon": 1.4, "tue": 1.4, "wed": 1.4, "thu": 1.4,
            "fri": 1.2, "sat": 0.6, "sun": 0.6,
        },
    }
    flat_total = _count_arrivals(None, seed=4242, days=days)
    peaked_total = _count_arrivals(peaked, seed=4242, days=days)
    # ~1500 arrivals over 30 days at 50/day; the means coincide, so the totals agree
    # within a few %% of sampling noise (the renorm guarantees equal expectation).
    assert abs(peaked_total - flat_total) / flat_total < 0.06, (
        flat_total,
        peaked_total,
    )


def test_curve_concentrates_arrivals_into_peak_hours() -> None:
    """A peaked diurnal curve lands more arrivals in peak hours than off-hours.

    Sanity that the curve actually *reshapes* the schedule (not just preserves the
    mean): the high-multiplier window receives disproportionately more arrivals.
    """
    curve = compile_intensity(
        {
            "diurnal": [
                {"from_hour": 0, "to_hour": 12, "multiplier": 0.2},
                {"from_hour": 12, "to_hour": 24, "multiplier": 1.8},
            ]
        }
    )
    tree = SeedTree(99)
    arrival = ArrivalProcess(Cursor(tree.key("transitions", "arrival:0")))
    vem = virtual_epoch_ms(VIRTUAL_EPOCH)
    rho = 200.0 / 86_400.0
    morning = afternoon = 0
    due = arrival.next_arrival_us_curved(rho, curve, vem)
    while due is not None and due <= 10 * _US_PER_DAY:
        hour = (due // 3_600_000_000) % 24
        if hour < 12:
            morning += 1
        else:
            afternoon += 1
        due = arrival.next_arrival_us_curved(rho, curve, vem)
    assert afternoon > morning * 3, (morning, afternoon)


# --------------------------------------------------------------------------- #
# Virtual-clock speed multiplier — segment arithmetic, pause/resume, position. #
# --------------------------------------------------------------------------- #


def test_multiplier_compresses_simulated_time() -> None:
    """kx compresses simulated time per wall second (segment arithmetic, §3).

    At k=60, one wall second advances virtual_now by 60 simulated seconds. The
    segment formula ``virtual_now = v_anchor + k.(wall - w_anchor)`` realizes the
    multiplier across dwell/latency/curve domains (all live on virtual time).
    """
    epoch = VIRTUAL_EPOCH
    wall0 = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    clock = VirtualClock(virtual_epoch=epoch, speed_multiplier=60.0)
    clock.open_segment(wall0)
    one_wall_second = wall0 + timedelta(seconds=1)
    assert clock.virtual_now_us(one_wall_second) == 60 * 1_000_000
    # 1x advances one-for-one.
    clock1x = VirtualClock(virtual_epoch=epoch, speed_multiplier=1.0)
    clock1x.open_segment(wall0)
    assert clock1x.virtual_now_us(one_wall_second) == 1_000_000


def test_pause_freezes_then_resume_rebases() -> None:
    """Pause freezes virtual time at F; resume rebases a fresh segment (§9.3 step 4).

    The paused wall gap is NOT counted — after resume, virtual_now continues from
    the frozen frontier, advancing only with post-resume wall time. This is what
    makes dwell timers (absolute virtual due-times) fire at the same instants.
    """
    epoch = VIRTUAL_EPOCH
    wall0 = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
    clock = VirtualClock(virtual_epoch=epoch, speed_multiplier=1.0)
    clock.open_segment(wall0)
    # advance to F = 10 s, mimic the engine advancing the frontier.
    clock.advance_frontier(10 * 1_000_000)
    clock.pause()
    assert clock.is_paused
    # 100 wall seconds pass while paused.
    wall_resume = wall0 + timedelta(seconds=110)
    clock.resume(wall_resume)
    assert not clock.is_paused
    # immediately after resume, virtual_now == frozen frontier (no paused gap).
    assert clock.virtual_now_us(wall_resume) == 10 * 1_000_000
    # one wall second after resume advances virtual by one second from F.
    assert clock.virtual_now_us(wall_resume + timedelta(seconds=1)) == 11 * 1_000_000


def test_clock_position_persists_across_freeze() -> None:
    """The checkpointed clock position is the frozen frontier (§9.1 vclock)."""
    clock = VirtualClock(virtual_epoch=VIRTUAL_EPOCH, speed_multiplier=10.0)
    clock.open_segment(datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC))
    clock.advance_frontier(42 * 1_000_000)
    clock.pause()
    assert clock.position_us == 42 * 1_000_000
    # a restored clock re-anchors at the persisted position and continues.
    restored = VirtualClock(
        virtual_epoch=VIRTUAL_EPOCH, speed_multiplier=10.0, frontier_us=clock.position_us
    )
    wall_resume = datetime(2026, 6, 14, 9, 0, 0, tzinfo=UTC)
    restored.resume(wall_resume)
    assert restored.virtual_now_us(wall_resume) == 42 * 1_000_000


# --------------------------------------------------------------------------- #
# Backfill — N simulated days, snapshot head, determinism with curves.         #
# --------------------------------------------------------------------------- #


def _manifest_with_intensity() -> dict[str, Any]:
    doc = copy.deepcopy(synthetic_manifest())
    doc["intensity"] = {
        "diurnal": [
            {"from_hour": 0, "to_hour": 9, "multiplier": 0.3},
            {"from_hour": 9, "to_hour": 21, "multiplier": 1.8},
            {"from_hour": 21, "to_hour": 24, "multiplier": 0.5},
        ],
        "weekly": {
            "mon": 1.2, "tue": 1.2, "wed": 1.2, "thu": 1.2,
            "fri": 1.1, "sat": 0.7, "sun": 0.6,
        },
    }
    return doc


def _backfill(doc: dict[str, Any], *, seed: int, days: int) -> list[Any]:
    ir = compile_manifest(doc)
    config = ShardConfig(
        seed=seed, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
        visits_per_actor_day=1.0,
    )
    shard = Shard(ir, config, FixedWallClock())
    until = days * _US_PER_DAY
    return shard.run_batch(until_us=until)


def test_backfill_head_is_snapshot_block() -> None:
    """The backfill body is led by the head op:"r" snapshot rows (§8 BE-F5)."""
    produced = _backfill(_manifest_with_intensity(), seed=7, days=5)
    assert produced, "backfill produced events"
    # the leading rows are op:"r" snapshots for the CDC-enabled seeded entities.
    assert produced[0]["op"] == "r", produced[0]["op"]
    # at least one non-snapshot event follows the snapshot head.
    assert any(e["op"] != "r" for e in produced)


def test_backfill_respects_window_end() -> None:
    """Generation stops at the window end; no occurred_at past +N days (§8 BE-F3)."""
    days = 7
    produced = _backfill(_manifest_with_intensity(), seed=11, days=days)
    window_end = VIRTUAL_EPOCH + timedelta(days=days)

    def _occurred(envelope: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(envelope["occurred_at"]).replace("Z", "+00:00"))

    assert all(_occurred(e) <= window_end for e in produced)
    # a longer window produces at least as many events (monotone in days).
    longer = _backfill(_manifest_with_intensity(), seed=11, days=days * 2)
    assert len(longer) >= len(produced)


def test_backfill_with_curves_is_deterministic() -> None:
    """Determinism holds with curves: same seed → byte-identical backfill (§8 BE-F1)."""
    from dataforge_engine.envelope import canonical_serialize

    a = _backfill(_manifest_with_intensity(), seed=2026, days=10)
    b = _backfill(_manifest_with_intensity(), seed=2026, days=10)
    assert [canonical_serialize(e) for e in a] == [canonical_serialize(e) for e in b]
    # a different seed diverges (the curve does not collapse the stream).
    c = _backfill(_manifest_with_intensity(), seed=9999, days=10)
    assert [canonical_serialize(e) for e in a] != [canonical_serialize(e) for e in c]
