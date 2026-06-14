"""Shard-worker reconciliation-tick tests (backend-architecture §8.3).

Unit-level coverage of the tick's reconcile branches with the engine + ORM seams
faked (the live kill-test / failover round-trip is the compose-only OPS suite, per
the Phase-5 CI note). Covered here:

* desired ``stopped`` → :meth:`ShardWorker._finalize` (T10): checkpoint retained,
  lifecycle converged to ``stopped``, the worker stops.
* a running tick emits in normative order: ledger append BEFORE publish (INV-GEN-5),
  publish keyed batch, stats incremented.
"""

from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest

from runner import lifecycle
from runner.leases import Lease, ShardKey
from runner.shard_worker import ShardWorker
from streams.application import desired_state
from streams.domain.models import RUN_RUNNING, RUN_STOPPED

pytestmark = pytest.mark.asyncio


class _FakeBucket:
    def __init__(self, grant: int) -> None:
        self._grant = grant
        self.rate = 0.0
        self.consumed = 0

    def set_rate(self, rate: float) -> None:
        self.rate = rate

    def refill(self, now: Any) -> None:
        return None

    def grant(self, now: Any) -> int:
        return self._grant

    def consume(self, count: int) -> None:
        self.consumed += count


class _RecordingLedger:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.appended: list[list[Any]] = []

    def append(self, batch: Any) -> None:
        self._order.append("ledger")
        self.appended.append(list(batch))


class _RecordingPublisher:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.published: list[list[Any]] = []

    def publish(self, batch: Any) -> int:
        self._order.append("publish")
        rows = list(batch)
        self.published.append(rows)
        return len(rows)


class _FakeCheckpoints:
    def __init__(self) -> None:
        self.saves = 0

    async def save(self, shard: Any, **kwargs: Any) -> None:
        self.saves += 1


def _worker() -> ShardWorker:
    shard_key = ShardKey.of("11111111-1111-1111-1111-111111111111", 0)
    lease = Lease(shard=shard_key, runner_id="runner-test", fencing_token=7)
    return ShardWorker(
        lease=lease,
        publisher=cast(Any, _RecordingPublisher([])),
        redis=cast(Any, mock.Mock()),
    )


def _desired(run_state: str) -> Any:
    return mock.Mock(run_state=run_state, target_tps=50, shard_count=1, chaos_config={})


async def test_tick_reconciles_stopped_to_finalize() -> None:
    """desired ``stopped`` → finalize (T10): checkpoint retained, lifecycle stopped."""
    worker = _worker()
    checkpoints = _FakeCheckpoints()
    worker._checkpoints = cast(Any, checkpoints)
    worker._shard = cast(Any, mock.Mock())
    worker._ledger = cast(Any, _RecordingLedger([]))
    worker._bucket = cast(Any, _FakeBucket(grant=100))

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_STOPPED)),
        mock.patch.object(lifecycle, "report_lifecycle") as report,
    ):
        stop = await worker._tick()

    assert stop is True  # the worker must stop after finalize
    assert checkpoints.saves == 1  # T10 checkpoint retained (T12 continuation)
    report.assert_awaited_once()
    await_args = report.await_args
    assert await_args is not None
    assert await_args.args[1] == lifecycle.STOPPED  # converged = stopped


async def test_running_tick_appends_ledger_before_publish() -> None:
    """A running tick: ledger.append BEFORE publish (INV-GEN-5), stats incremented."""
    order: list[str] = []
    worker = _worker()
    worker._checkpoints = cast(Any, _FakeCheckpoints())
    worker._ledger = cast(Any, _RecordingLedger(order))
    worker._publisher = cast(Any, _RecordingPublisher(order))
    worker._bucket = cast(Any, _FakeBucket(grant=100))
    worker._shard_count = 1

    batch = [{"partition_key": "ws:stream:users:a"}]
    shard = mock.Mock()
    shard.clock.virtual_now_us.return_value = 1_000_000
    shard.generate.return_value = batch
    worker._shard = cast(Any, shard)

    with (
        mock.patch.object(desired_state, "desired_for", return_value=_desired(RUN_RUNNING)),
        mock.patch.object(lifecycle, "incr_emitted") as incr,
    ):
        stop = await worker._tick()

    assert stop is False
    assert order == ["ledger", "publish"]  # INV-GEN-5: ledger is durable first
    incr.assert_awaited_once()  # §8.3 step 9 stats
    assert worker.emitted_total == 1
