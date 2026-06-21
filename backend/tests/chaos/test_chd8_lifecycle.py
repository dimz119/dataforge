"""CHD-8 — the late-buffer pause/resume lifecycle binding (§10.3, INV-CHA-5).

Phase-9 exit criterion #5 (merge): a paused stream resumes with pending late
re-emissions intact. The per-case §6.3 matrix (steady-state hold, discard/flush
on stop, failover takeover, idempotent reschedule) is covered exhaustively in
``test_late_buffer.py`` (the CHD-8 binding); this module pins the multi-entry
pause→resume scenario end-to-end:

1. schedule several pending re-emissions (a tick's late selections);
2. PAUSE: the scheduler does not poll — every entry is held verbatim, ``due_at``
   not rebased (a pause never stretches the pending wall schedule);
3. RESUME: a poll at a time past every ``due_at`` drains ALL pending entries
   promptly, in ``due_at`` order, each re-emitted with the OLD ``occurred_at`` and
   a NEW ``emitted_at`` (INV-CHA-6).

Postgres/SQLite-agnostic via the ORM (the durability is the row, not the vendor);
rides the ``django_db`` + ``chaos`` markers (the Postgres/golden CI lanes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from chaos.domain.models import BUFFER_EMITTED, BUFFER_PENDING, LateArrivalBufferEntry
from chaos.infra.late_buffer import LateArrivalBuffer
from chaos.infra.recorder import InjectionRecorder
from tenancy.application.services import worker_workspace_scope

from .conftest import SHARD_ID, STREAM_ID, ChaosWorld, make_entry, make_injection

pytestmark = [pytest.mark.django_db, pytest.mark.chaos]

# Three pending entries due during the (simulated) pause window.
_DUE_TIMES = (
    "2026-06-10T14:30:00.000000Z",
    "2026-06-10T14:35:00.000000Z",
    "2026-06-10T14:40:00.000000Z",
)
_PAUSE_POLL = datetime(2026, 6, 10, 14, 33, 0, tzinfo=UTC)  # mid-window (not all due)
_RESUME_POLL = datetime(2026, 6, 10, 15, 0, 0, tzinfo=UTC)  # past every due_at


class _Publisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, batch: list[dict[str, Any]]) -> int:
        self.published.extend(batch)
        return len(batch)


def _seed_many(world: ChaosWorld) -> None:
    """Record + schedule three pending late re-emissions (a tick's selections)."""
    rec = InjectionRecorder()
    entries = []
    for i, due in enumerate(_DUE_TIMES):
        event_id = f"bbbbbbbb-0000-7000-8000-00000000000{i}"
        injection = make_injection(str(world.workspace.id), event_id, due_at=due)
        rec.record(injection)
        entries.append(make_entry(injection, due))
    rec.flush()
    LateArrivalBuffer(
        workspace_id=str(world.workspace.id),
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        publish=lambda b: len(b),
    ).schedule(entries)


def test_chd8_pause_holds_all_pending_then_resume_emits_all(chaos_world: ChaosWorld) -> None:
    """CHD-8: pause holds pending re-emissions; resume drains them all (INV-CHA-5)."""
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed_many(chaos_world)
        # PAUSE: the scheduler does not poll. We assert the durable state directly —
        # all three rows are pending, due_at untouched (not rebased by the pause).
        pending = list(LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID))
        assert len(pending) == 3
        assert all(p.state == BUFFER_PENDING for p in pending)
        held_due = sorted(p.due_at.isoformat() for p in pending)

        # RESUME: a single poll past every due_at drains ALL pending entries.
        buf = LateArrivalBuffer(
            workspace_id=str(chaos_world.workspace.id),
            stream_id=STREAM_ID,
            shard_id=SHARD_ID,
            publish=pub.publish,
        )
        drained = buf.take_due(_RESUME_POLL)
        rows = list(LateArrivalBufferEntry.objects.filter(stream_id=STREAM_ID))

    assert drained == 3  # every pending entry emitted on resume
    assert all(r.state == BUFFER_EMITTED for r in rows)
    assert len(pub.published) == 3
    # due_at was NOT rebased by the pause (held verbatim).
    assert sorted(r.due_at.isoformat() for r in rows) == held_due
    # Each re-emission carries the OLD occurred_at and a NEW (resume-time) emitted_at.
    for env in pub.published:
        assert env["occurred_at"] == "2026-06-10T14:00:00.000000Z"
        assert env["emitted_at"].startswith("2026-06-10T15:00:00")


def test_chd8_partial_pause_poll_holds_not_yet_due(chaos_world: ChaosWorld) -> None:
    """CHD-8: a poll mid-window emits only the overdue subset; the rest stay pending."""
    pub = _Publisher()
    with worker_workspace_scope(chaos_world.workspace.id):
        _seed_many(chaos_world)
        # A poll at 14:33 — only the 14:30 entry is due; the 14:35/14:40 are held.
        emitted = LateArrivalBuffer(
            workspace_id=str(chaos_world.workspace.id),
            stream_id=STREAM_ID,
            shard_id=SHARD_ID,
            publish=pub.publish,
        ).take_due(_PAUSE_POLL)
        still_pending = LateArrivalBufferEntry.objects.filter(
            stream_id=STREAM_ID, state=BUFFER_PENDING
        ).count()
    assert emitted == 1
    assert still_pending == 2  # not-yet-due entries held (INV-CHA-5)
