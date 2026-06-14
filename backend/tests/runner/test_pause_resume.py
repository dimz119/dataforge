"""Shard-worker pause/resume + dynamic-TPS reconcile tests (Phase 6; backend-arch §8.3).

The runner half of the Phase-6 stream-control contract, exercised at the tick level
with the engine + ORM seams faked (the live OPS round-trip is the compose-only suite).
Covered:

* **Pause (T5→T6):** desired ``paused`` → emission halts within ONE tick (no
  generate/publish), a checkpoint is persisted SYNCHRONOUSLY before reporting
  ``paused``, the lease is RETAINED (the worker idles, does not stop), and subsequent
  paused ticks idle without re-checkpointing.
* **Resume (T7→T8):** desired ``running`` again → the clock segment is re-anchored
  (dwell rebase, §9.3 step 4), lifecycle is reported ``running`` again, and the tick
  proceeds to generate. The gapless ``sequence_no`` continues (warm state in memory).
* **Stop-override (T9):** a desired ``stopped`` seen while paused finalizes (T10) and
  the worker stops — stop wins over the pause hold.
* **Dynamic TPS (§3.6 BE-P2):** each running tick applies ``desired.target_tps`` to
  both the token bucket (wall rate) and the engine live slot (arrival density).
"""

from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest

from runner import lifecycle
from runner.leases import Lease, ShardKey
from runner.shard_worker import ShardWorker
from streams.application import desired_state
from streams.domain.models import LC_PAUSED, RUN_PAUSED, RUN_RUNNING, RUN_STOPPED

pytestmark = pytest.mark.asyncio


class _FakeBucket:
    def __init__(self, grant: int = 100) -> None:
        self._grant = grant
        self.rate = 0.0
        self.tokens = float(grant)
        self.capacity = float(grant)
        self.consumed = 0

    def set_rate(self, rate: float) -> None:
        self.rate = rate

    def refill(self, now: Any) -> None:
        return None

    def grant(self, now: Any) -> int:
        return self._grant

    def consume(self, count: int) -> None:
        self.consumed += count


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[list[Any]] = []

    def publish(self, batch: Any) -> int:
        rows = list(batch)
        self.published.append(rows)
        return len(rows)


class _RecordingLedger:
    def __init__(self) -> None:
        self.appended: list[list[Any]] = []

    def append(self, batch: Any) -> None:
        self.appended.append(list(batch))


class _FakeCheckpoints:
    def __init__(self) -> None:
        self.saves = 0

    async def save(self, shard: Any, **kwargs: Any) -> None:
        self.saves += 1


def _worker() -> ShardWorker:
    shard_key = ShardKey.of("11111111-1111-1111-1111-111111111111", 0)
    lease = Lease(shard=shard_key, runner_id="runner-test", fencing_token=7)
    worker = ShardWorker(
        lease=lease,
        publisher=cast(Any, _RecordingPublisher()),
        redis=cast(Any, mock.Mock()),
    )
    worker._checkpoints = cast(Any, _FakeCheckpoints())
    worker._bucket = cast(Any, _FakeBucket())
    worker._shard_count = 1
    # Empty workspace id → the ledger append takes the unarmed branch (no DB / RLS is
    # a SQLite no-op anyway) so the tick-level unit tests stay hermetic (no django_db).
    worker._workspace_id = ""
    return worker


def _desired(run_state: str, *, target_tps: int = 50, status_reason: str = "user") -> Any:
    return mock.Mock(
        run_state=run_state,
        target_tps=target_tps,
        shard_count=1,
        chaos_config={},
        status_reason=status_reason,
    )


def _running_shard() -> Any:
    shard = mock.Mock()
    shard.clock.virtual_now_us.return_value = 1_000_000
    shard.clock.frontier_us = 1_000_000
    shard.generate.return_value = [{"partition_key": "ws:stream:users:a"}]
    return shard


# --- Pause (T5→T6) ----------------------------------------------------------


async def test_pause_halts_within_one_tick_checkpoints_and_retains_lease() -> None:
    """desired paused → no emission this tick, synchronous checkpoint, lease retained."""
    worker = _worker()
    shard: Any = _running_shard()
    worker._shard = shard
    worker._ledger = cast(Any, _RecordingLedger())
    checkpoints = cast(_FakeCheckpoints, worker._checkpoints)
    publisher = cast(_RecordingPublisher, worker._publisher)

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_PAUSED)),
        mock.patch.object(lifecycle, "report_lifecycle") as report,
    ):
        stop = await worker._tick()

    assert stop is False  # the worker IDLES (lease retained) — it does not stop
    assert worker._paused is True
    assert checkpoints.saves == 1  # synchronous checkpoint on pause entry (T6)
    assert publisher.published == []  # emission halted within one tick (no publish)
    shard.generate.assert_not_called()  # nothing generated this tick
    # lifecycle converged to paused, preserving the desired status_reason.
    report.assert_awaited_once()
    args = report.await_args
    assert args is not None
    assert args.args[1] == LC_PAUSED
    assert args.kwargs["status_reason"] == "user"


async def test_paused_idle_tick_does_not_recheckpoint_or_emit() -> None:
    """A subsequent paused tick idles: no second checkpoint, no emission, no stop."""
    worker = _worker()
    shard: Any = _running_shard()
    worker._shard = shard
    worker._ledger = cast(Any, _RecordingLedger())
    worker._paused = True  # already converged to paused
    checkpoints = cast(_FakeCheckpoints, worker._checkpoints)

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_PAUSED)),
        mock.patch.object(lifecycle, "report_lifecycle"),
    ):
        stop = await worker._tick()

    assert stop is False
    assert checkpoints.saves == 0  # no re-checkpoint while idling
    shard.generate.assert_not_called()


async def test_pause_preserves_system_status_reason() -> None:
    """A system pause (status_reason=quota) is converged with that reason (Phase 11)."""
    worker = _worker()
    worker._shard = _running_shard()
    worker._ledger = cast(Any, _RecordingLedger())

    with (
        mock.patch.object(
            desired_state,
            "desired_for",
            return_value=_desired(RUN_PAUSED, status_reason="quota"),
        ),
        mock.patch.object(lifecycle, "report_lifecycle") as report,
    ):
        await worker._tick()

    assert report.await_args is not None
    assert report.await_args.kwargs["status_reason"] == "quota"


# --- Resume (T7→T8) ---------------------------------------------------------


async def test_resume_reanchors_clock_and_reports_running() -> None:
    """desired running while paused → clock re-anchored (rebase), running reported."""
    worker = _worker()
    shard = _running_shard()
    worker._shard = shard
    worker._ledger = cast(Any, _RecordingLedger())
    worker._paused = True  # currently paused

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_RUNNING)),
        mock.patch.object(lifecycle, "report_lifecycle") as report,
        mock.patch.object(lifecycle, "incr_emitted"),
    ):
        stop = await worker._tick()

    assert stop is False
    assert worker._paused is False  # resumed
    shard.reopen_clock_segment.assert_called_once()  # dwell rebase (§9.3 step 4)
    # lifecycle reported running again (T8), then the tick generated + published.
    assert report.await_args is not None
    assert report.await_args.args[1] == lifecycle.RUNNING
    shard.generate.assert_called_once()


# --- Stop-override (T9) -----------------------------------------------------


async def test_stop_overrides_pause() -> None:
    """desired stopped while paused → finalize (T10), worker stops (stop wins, T9)."""
    worker = _worker()
    worker._shard = cast(Any, mock.Mock())
    worker._ledger = cast(Any, _RecordingLedger())
    worker._paused = True  # was paused; a stop now arrives

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_STOPPED)),
        mock.patch.object(lifecycle, "report_lifecycle") as report,
    ):
        stop = await worker._tick()

    assert stop is True  # the worker finalizes and stops — stop overrode the pause
    assert cast(_FakeCheckpoints, worker._checkpoints).saves == 1  # T10 checkpoint retained
    assert report.await_args is not None
    assert report.await_args.args[1] == lifecycle.STOPPED


async def test_stop_overrides_while_pausing_not_yet_converged() -> None:
    """A stop seen before the pause converged also finalizes (T9 over pausing)."""
    worker = _worker()
    worker._shard = cast(Any, mock.Mock())
    worker._ledger = cast(Any, _RecordingLedger())
    worker._paused = False  # pause not yet converged

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_STOPPED)),
        mock.patch.object(lifecycle, "report_lifecycle"),
    ):
        stop = await worker._tick()

    assert stop is True


# --- Dynamic TPS (§3.6 BE-P2) ----------------------------------------------


async def test_running_tick_applies_target_tps_to_bucket_and_engine() -> None:
    """A running tick sets the bucket wall-rate AND the engine live TPS (BE-P2)."""
    worker = _worker()
    shard = _running_shard()
    worker._shard = shard
    worker._ledger = cast(Any, _RecordingLedger())
    bucket = cast(_FakeBucket, worker._bucket)

    with (
        mock.patch.object(
            desired_state, "desired_for", return_value=_desired(RUN_RUNNING, target_tps=500)
        ),
        mock.patch.object(lifecycle, "incr_emitted"),
    ):
        await worker._tick()

    assert bucket.rate == 500.0  # rate = target_tps / shard_count (1)
    shard.set_target_tps.assert_called_once_with(500.0)  # engine arrival density (BE-P2)
