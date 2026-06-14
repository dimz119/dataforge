"""StreamStats rebuild-from-buffer tests (observability §5; INV-OBS-2 rebuildable).

Counters are derivable from ``event_buffer`` — a Redis loss loses no durable truth.
``manage.py rebuild_stream_stats`` reconstructs the total / per-type / last_event_at
from the buffer rows the buffer-writer committed; the rebuilt tally must equal what
live counting produced (the buffer-writer counts exactly the rows it commits).

Hermetic SQLite + fakeredis (``fake_stats_redis`` autouse).
"""

from __future__ import annotations

import pytest

from dataforge_engine.envelope.tests.fixtures import WORKSPACE_ID
from delivery.infra import buffer_stats, stream_stats
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import make_batch, make_internal_event

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _arm(armed_workspace: str) -> None:
    """Arm the engine-fixture workspace for the channel's RLS-scoped writes."""


def _live_count(stream_id: str) -> stream_stats.StreamStatsSnapshot:
    return stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)


def test_tally_from_buffer_matches_written_rows(stream_id: str) -> None:
    """``tally_from_buffer`` reconstructs total/per-type from the committed rows."""
    channel = BufferWriterChannel()
    channel.deliver(make_batch([make_internal_event(seq_offset=i) for i in range(6)]))

    tally = buffer_stats.tally_from_buffer(
        stream_id=stream_id, tps_window_s=stream_stats.TPS_WINDOW_S
    )
    assert tally.total_events == 6
    assert tally.by_event_type == {"order_placed": 6}
    assert tally.last_event_at is not None


def test_rebuild_reproduces_live_counts(stream_id: str) -> None:
    """After a Redis wipe, rebuild reproduces the same total/per-type the sink counted."""
    channel = BufferWriterChannel()
    channel.deliver(make_batch([make_internal_event(seq_offset=i) for i in range(8)]))

    before = _live_count(stream_id)
    assert before.total_events == 8

    # Simulate a Redis loss: drop the live counters.
    hkey = stream_stats.stats_hash_key(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    rkey = stream_stats.tps_ring_key(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    stream_stats._redis().delete(hkey, rkey)
    assert _live_count(stream_id).present is False

    # Resolve the stream's workspace for the command (the unscoped row read needs a
    # Stream row; the engine-fixture has no Stream row, so drive the rebuild helper
    # directly under the armed workspace — the command's per-stream body).
    tally = buffer_stats.tally_from_buffer(
        stream_id=stream_id, tps_window_s=stream_stats.TPS_WINDOW_S
    )
    stream_stats.write_rebuilt_stats(
        workspace_id=WORKSPACE_ID,
        stream_id=stream_id,
        total_events=tally.total_events,
        by_event_type=tally.by_event_type,
        last_event_at=tally.last_event_at,
        tps_ring_ms=tally.recent_emitted_ms,
    )

    after = _live_count(stream_id)
    assert after.present is True
    assert after.total_events == before.total_events == 8
    assert after.by_event_type == before.by_event_type == {"order_placed": 8}
