"""Runner ↔ chaos wiring tests (chaos-engine §6.3 lifecycle; django_db).

Drives the synchronous chaos seam methods :meth:`ShardWorker._run_chaos`,
:meth:`ShardWorker._drain_due`, and :meth:`ShardWorker._run_on_stop` (the bodies
``_emit``/``_take_due_late``/``_apply_on_stop_policy`` wrap in ``to_thread`` — that
async glue is exercised by ``tests/runner``) to prove the §8.3 step-5/6/7 wiring
with the REAL pipeline + Postgres ports: the pipeline records injections + extracts
late selections into the durable buffer (held until due), the scheduler re-emits
due entries (resume), and stop applies the OnStopPolicy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest import mock

import pytest

from chaos.infra.late_buffer import LateArrivalBuffer
from chaos.infra.recorder import InjectionRecorder
from dataforge_engine.chaos import ChaosPipeline, chaos_subseed
from dataforge_engine.chaos.tests.fixtures import make_batch
from runner.leases import Lease, ShardKey
from runner.shard_worker import ShardWorker
from tenancy.application.services import worker_workspace_scope

from .conftest import SHARD_ID, STREAM_ID, ChaosWorld

pytestmark = pytest.mark.django_db


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _Pub:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, batch: Any) -> int:
        rows = list(batch)
        self.published.extend(rows)
        return len(rows)


_LATE_POLICY = {
    "late_arriving": {
        "enabled": True,
        "rate": 0.5,  # high rate so the small batch selects some
        "params": {
            "delay": {"family": "fixed", "value": "PT30M"},
            "max_delay": "PT24H",
            "event_types": ["*"],
        },
    },
    "on_stop_policy": "discard",
}


def _wire_worker(world: ChaosWorld, pub: _Pub, now: datetime) -> ShardWorker:
    from chaos.application.services import resolve_policy

    lease = Lease(shard=ShardKey.of(STREAM_ID, SHARD_ID), runner_id="r", fencing_token=1)
    worker = ShardWorker(lease=lease, publisher=cast(Any, pub), redis=cast(Any, mock.Mock()))
    worker._workspace_id = str(world.workspace.id)
    worker._shard_count = 1
    worker._speed_multiplier = 1.0
    worker._chaos = ChaosPipeline(resolve_policy(_LATE_POLICY))
    worker._chaos_subseed = chaos_subseed(424242)
    worker._recorder = InjectionRecorder()
    worker._late_buffer = LateArrivalBuffer(
        workspace_id=str(world.workspace.id),
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        publish=pub.publish,
        speed_multiplier=1.0,
    )
    worker._shard = cast(Any, mock.Mock())
    worker._ledger = cast(Any, mock.Mock())
    worker._wall = cast(Any, _Clock(now))
    return worker


def _late_batch(n: int = 20) -> list[Any]:
    batch = make_batch(n)
    for env in batch:  # pin emitted_at == occurred_at so due_at = +30 min exactly
        env["emitted_at"] = env["occurred_at"]
    return batch


def test_run_chaos_records_and_extracts_then_drains_on_due(chaos_world: ChaosWorld) -> None:
    """A tick: lates are recorded + extracted (held); a later poll re-emits them."""
    from chaos.domain.models import ChaosInjection, LateArrivalBufferEntry

    t0 = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)
    pub = _Pub()
    worker = _wire_worker(chaos_world, pub, now=t0)

    with worker_workspace_scope(chaos_world.workspace.id):
        out = worker._run_chaos(_late_batch())  # transform + record flush + schedule
        pending = list(LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID))
        injections = list(ChaosInjection.objects.filter(stream_id=STREAM_ID))
    assert len(pending) > 0  # some extracted into the durable buffer
    assert all(p.state == "pending" for p in pending)
    assert len(injections) == len(pending)  # recorded before extraction (INV-CHA-4)
    assert len(out) == 20 - len(pending)  # extracted lates left the in-line flow

    # Advance the clock past due_at (+31 min) and poll again (resume / next tick).
    worker._wall = cast(Any, _Clock(t0.replace(minute=31)))
    with worker_workspace_scope(chaos_world.workspace.id):
        drained = worker._drain_due()
        emitted = LateArrivalBufferEntry.objects.filter(
            stream_id=STREAM_ID, state="emitted"
        ).count()
    assert drained == len(pending)
    assert emitted == len(pending)


def test_run_chaos_holds_when_not_due(chaos_world: ChaosWorld) -> None:
    """At t0 (before due_at) the scheduler holds every entry (steady-state/pause)."""
    from chaos.domain.models import LateArrivalBufferEntry

    t0 = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)
    worker = _wire_worker(chaos_world, _Pub(), now=t0)
    with worker_workspace_scope(chaos_world.workspace.id):
        worker._run_chaos(_late_batch())
        drained = worker._drain_due()  # nothing due at t0
        held = LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID, state="pending").count()
    assert drained == 0
    assert held > 0  # held verbatim (INV-CHA-5)


def test_on_stop_discard_marks_pending_discarded(chaos_world: ChaosWorld) -> None:
    """_run_on_stop('discard') marks pending entries discarded (§6.3 default)."""
    from chaos.domain.models import LateArrivalBufferEntry

    t0 = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)
    worker = _wire_worker(chaos_world, _Pub(), now=t0)
    with worker_workspace_scope(chaos_world.workspace.id):
        worker._run_chaos(_late_batch())
        worker._run_on_stop("discard")
        states = set(
            LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID).values_list(
                "state", flat=True
            )
        )
    assert states == {"discarded"}


def test_on_stop_flush_publishes_pending(chaos_world: ChaosWorld) -> None:
    """_run_on_stop('flush') publishes every pending entry now (§6.3)."""
    from chaos.domain.models import LateArrivalBufferEntry

    t0 = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)
    pub = _Pub()
    worker = _wire_worker(chaos_world, pub, now=t0)
    with worker_workspace_scope(chaos_world.workspace.id):
        worker._run_chaos(_late_batch())
        pending = LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID).count()
        flushed = worker._run_on_stop("flush")
        emitted = LateArrivalBufferEntry.objects.filter(
            stream_id=STREAM_ID, state="emitted"
        ).count()
    assert flushed == pending  # all published despite not being due
    assert emitted == pending
