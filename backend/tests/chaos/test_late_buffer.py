"""Durable late-arrival buffer lifecycle tests (chaos-engine §6; django_db).

Covers the §6.3 matrix end to end against Postgres/SQLite via the ORM:

* schedule persists a ``pending`` row (durable, INV-CHA-5);
* take_due returns + marks due entries, re-emitting with the OLD ``occurred_at``
  and a NEW ``emitted_at`` (INV-CHA-6) and recording the realized delay (§6.4);
* a not-yet-due entry is held (the steady-state / pause hold — scheduler skips it);
* stop OnStopPolicy: discard (default) → ``discarded``; flush → ``emitted``
  (``outcome: flushed``);
* failover: a FRESH buffer instance (new lease holder) drains the durable pending
  entries (§6.3 failover row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from chaos.domain.models import (
    BUFFER_DISCARDED,
    BUFFER_EMITTED,
    ChaosInjection,
    LateArrivalBufferEntry,
)
from chaos.infra.late_buffer import LateArrivalBuffer
from chaos.infra.recorder import InjectionRecorder
from tenancy.application.services import worker_workspace_scope

from .conftest import SHARD_ID, STREAM_ID, ChaosWorld, make_entry, make_injection

# CHD-8 (testing-strategy §10.3): this module IS the late-buffer lifecycle binding
# — pause-hold / resume-prompt-emit / stop OnStopPolicy discard|flush / failover.
pytestmark = [pytest.mark.django_db, pytest.mark.chaos]

_NOW = datetime(2026, 6, 10, 14, 31, 0, tzinfo=UTC)  # 1 min past a 14:30 due_at
_DUE = "2026-06-10T14:30:00.000000Z"
_FUTURE = "2026-06-10T20:00:00.000000Z"  # well past _NOW → held


class _Publisher:
    def __init__(self) -> None:
        self.published: list[list[dict[str, Any]]] = []

    def publish(self, batch: list[dict[str, Any]]) -> int:
        self.published.append(batch)
        return len(batch)


def _buffer(world: ChaosWorld, pub: _Publisher, k: float = 1.0) -> LateArrivalBuffer:
    return LateArrivalBuffer(
        workspace_id=str(world.workspace.id),
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        publish=pub.publish,
        speed_multiplier=k,
    )


def _seed(world: ChaosWorld, event_id: str, due_at: str) -> None:
    """Record the injection + schedule the buffer entry (the §5.7 extract path)."""
    injection = make_injection(str(world.workspace.id), event_id, due_at=due_at)
    rec = InjectionRecorder()
    rec.record(injection)
    rec.flush()
    LateArrivalBuffer(
        workspace_id=str(world.workspace.id),
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        publish=lambda b: len(b),
        speed_multiplier=1.0,
    ).schedule([make_entry(injection, due_at)])


def test_scheduled_entry_persists_pending(chaos_world: ChaosWorld) -> None:
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000001", _DUE)
        rows = list(LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID))
    assert len(rows) == 1
    assert rows[0].state == "pending"
    assert rows[0].resolved_at is None


def test_take_due_returns_due_and_records_realized_delay(chaos_world: ChaosWorld) -> None:
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000002", _DUE)
        count = _buffer(chaos_world, pub).take_due(_NOW)
        entry = LateArrivalBufferEntry.objects.get(stream_id=STREAM_ID)
        injection = ChaosInjection.objects.get(event_id=entry.event_id)
    assert count == 1
    assert entry.state == BUFFER_EMITTED
    assert entry.resolved_at is not None
    # Re-emitted with the OLD occurred_at and a NEW emitted_at (INV-CHA-6).
    published = pub.published[0][0]
    assert published["occurred_at"] == "2026-06-10T14:00:00.000000Z"
    assert published["emitted_at"].startswith("2026-06-10T14:31:00")
    # Realized delay recorded (§6.4): ~31 min from canonical emitted_at to now.
    assert injection.details["outcome"] == "emitted"
    assert injection.details["realized_wall_delay_ms"] == 31 * 60 * 1000


def test_not_yet_due_is_held(chaos_world: ChaosWorld) -> None:
    """A future entry is NOT returned by take_due (steady-state / pause hold)."""
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000003", _FUTURE)
        count = _buffer(chaos_world, pub).take_due(_NOW)
        entry = LateArrivalBufferEntry.objects.get(stream_id=STREAM_ID)
    assert count == 0
    assert entry.state == "pending"  # held, untouched (INV-CHA-5)
    assert pub.published == []


def test_resume_emits_overdue_promptly(chaos_world: ChaosWorld) -> None:
    """An entry whose due_at passed during a pause emits on the next poll (§6.3)."""
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000004", _DUE)
        # "Resume" = the scheduler polls again; the overdue entry drains now.
        count = _buffer(chaos_world, pub).take_due(_NOW)
    assert count == 1
    assert len(pub.published) == 1


def test_stop_discard_marks_discarded(chaos_world: ChaosWorld) -> None:
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000005", _FUTURE)
        emitted = _buffer(chaos_world, pub).discard_pending(_NOW)
        entry = LateArrivalBufferEntry.objects.get(stream_id=STREAM_ID)
        injection = ChaosInjection.objects.get(event_id=entry.event_id)
    assert emitted == 1  # one pending discarded
    assert entry.state == BUFFER_DISCARDED
    assert injection.details["outcome"] == "discarded"
    assert pub.published == []  # discard never publishes


def test_stop_flush_publishes_all_pending(chaos_world: ChaosWorld) -> None:
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        # A FUTURE entry: flush publishes it anyway (ignores due_at, §6.3).
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000006", _FUTURE)
        count = _buffer(chaos_world, pub).flush_pending(_NOW)
        entry = LateArrivalBufferEntry.objects.get(stream_id=STREAM_ID)
        injection = ChaosInjection.objects.get(event_id=entry.event_id)
    assert count == 1
    assert entry.state == BUFFER_EMITTED
    assert injection.details["outcome"] == "flushed"  # distinguishes early publish
    assert len(pub.published) == 1


def test_failover_fresh_buffer_takes_pending(chaos_world: ChaosWorld) -> None:
    """A FRESH buffer instance (new lease holder) drains durable pending (§6.3)."""
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed(chaos_world, "aaaaaaaa-0000-7000-8000-000000000007", _DUE)
    # Simulate a crash: a brand-new LateArrivalBuffer (no in-memory state) is built
    # under the "new lease holder" and polls the durable table.
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        fresh = _buffer(chaos_world, pub)
        count = fresh.take_due(_NOW)
        entry = LateArrivalBufferEntry.objects.get(stream_id=STREAM_ID)
    assert count == 1  # picked up without any hand-off
    assert entry.state == BUFFER_EMITTED
    assert len(pub.published) == 1


def test_schedule_idempotent_on_injection_id(chaos_world: ChaosWorld) -> None:
    """A tick retry re-schedules the same injection_id without duplicating (CR-7)."""
    with worker_workspace_scope(chaos_world.workspace.id):
        injection = make_injection(
            str(chaos_world.workspace.id), "aaaaaaaa-0000-7000-8000-000000000008", due_at=_DUE
        )
        rec = InjectionRecorder()
        rec.record(injection)
        rec.flush()
        entry = make_entry(injection, _DUE)
        buf = LateArrivalBuffer(
            workspace_id=str(chaos_world.workspace.id),
            stream_id=STREAM_ID,
            shard_id=SHARD_ID,
            publish=lambda b: len(b),
        )
        buf.schedule([entry])
        buf.schedule([entry])  # retry
        rows = LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID).count()
    assert rows == 1
