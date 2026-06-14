"""StreamStats counter unit tests (observability §5; INV-OBS-2/INV-OBS-3).

The Redis-resident per-stream counters on the canonical buffer-writer sink path:

* **counters increment on ingest** — a buffer-writer ``deliver`` bumps
  ``total_events`` + the per-event-type counts and stamps ``last_event_at`` (the
  delivered tally that reconciles with REST truth);
* **read shape** — :func:`read_stats` returns the snapshot the API renders;
* **observed_tps reflects rate** — instances inside the trailing 10 s window divide
  to the rolling rate; events older than the window do not count;
* **workspace/stream-labeled keys** (INV-OBS-3) and **fail-open** writes (INV-OBS-2).

Hermetic SQLite + fakeredis (``fake_stats_redis`` autouse): no broker, no live Redis.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dataforge_engine.envelope.tests.fixtures import STREAM_ID, WORKSPACE_ID
from delivery.infra import stream_stats
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import make_batch, make_internal_event

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _arm(armed_workspace: str) -> None:
    """Arm the engine-fixture workspace for the channel's RLS-scoped writes."""


def _now_iso(offset_s: float = 0.0) -> str:
    dt = datetime.now(UTC) + timedelta(seconds=offset_s)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond:06d}Z"


def test_deliver_increments_total_and_per_type(stream_id: str) -> None:
    """A buffer-writer ``deliver`` bumps total + per-type counts (the ingest hook)."""
    channel = BufferWriterChannel()
    events = [make_internal_event(seq_offset=i) for i in range(5)]
    result = channel.deliver(make_batch(events))
    assert result.status == "ok"

    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    assert snap.present is True
    assert snap.total_events == 5
    # The conformance events are all ``order_placed``.
    assert snap.by_event_type == {"order_placed": 5}
    assert snap.last_event_at is not None


def test_counts_accumulate_across_batches(stream_id: str) -> None:
    """Counters are additive across deliveries (HINCRBY, not overwrite)."""
    channel = BufferWriterChannel()
    channel.deliver(make_batch([make_internal_event(seq_offset=i) for i in range(3)]))
    channel.deliver(
        make_batch([make_internal_event(seq_offset=i) for i in range(3, 7)], first_offset=3)
    )
    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    assert snap.total_events == 7
    assert snap.by_event_type == {"order_placed": 7}


def test_record_delivered_batch_directly_multi_type(stream_id: str) -> None:
    """Mixed event types fan out to one per-type field each (the by_event_type map)."""
    envelopes: list[dict[str, Any]] = [
        {"event_type": "product_viewed", "emitted_at": _now_iso()},
        {"event_type": "product_viewed", "emitted_at": _now_iso()},
        {"event_type": "order_placed", "emitted_at": _now_iso()},
    ]
    stream_stats.record_delivered_batch(
        workspace_id=WORKSPACE_ID, stream_id=stream_id, envelopes=envelopes
    )
    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    assert snap.total_events == 3
    assert snap.by_event_type == {"product_viewed": 2, "order_placed": 1}


def test_observed_tps_reflects_recent_rate(stream_id: str) -> None:
    """Instances inside the 10 s window divide to the rolling rate (observed_tps)."""
    # 30 instances stamped 'now' → 30 / 10 s window = 3.0 tps.
    envelopes = [{"event_type": "e", "emitted_at": _now_iso()} for _ in range(30)]
    stream_stats.record_delivered_batch(
        workspace_id=WORKSPACE_ID, stream_id=stream_id, envelopes=envelopes
    )
    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    assert snap.observed_tps == pytest.approx(3.0, abs=0.2)


def test_observed_tps_excludes_stale_events(stream_id: str) -> None:
    """Events older than the window do not count toward observed_tps (rolling)."""
    old = [{"event_type": "e", "emitted_at": _now_iso(offset_s=-120)} for _ in range(50)]
    stream_stats.record_delivered_batch(
        workspace_id=WORKSPACE_ID, stream_id=stream_id, envelopes=old
    )
    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=stream_id)
    # total still counts them; the TPS ring window does not.
    assert snap.total_events == 50
    assert snap.observed_tps == pytest.approx(0.0, abs=0.01)


def test_keys_are_workspace_and_stream_labeled() -> None:
    """The Redis keys embed both workspace_id and stream_id (INV-OBS-3 / INV-TEN-1)."""
    hkey = stream_stats.stats_hash_key(workspace_id=WORKSPACE_ID, stream_id=STREAM_ID)
    rkey = stream_stats.tps_ring_key(workspace_id=WORKSPACE_ID, stream_id=STREAM_ID)
    for key in (hkey, rkey):
        assert WORKSPACE_ID in key
        assert STREAM_ID in key
    assert hkey != rkey


def test_read_absent_stream_is_not_present() -> None:
    """A stream that never delivered reads as an absent snapshot (health=degraded)."""
    snap = stream_stats.read_stats(workspace_id=WORKSPACE_ID, stream_id=str(uuid.uuid4()))
    assert snap.present is False
    assert snap.total_events == 0
    assert snap.observed_tps == 0.0
    assert snap.last_event_at is None


def test_record_failopen_on_redis_error(
    stream_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Redis error during the write is swallowed — a delivery is never failed (INV-OBS-2)."""
    import redis

    def _boom() -> Any:
        raise redis.RedisError("down")

    monkeypatch.setattr(stream_stats, "_redis", _boom)
    # Must not raise.
    stream_stats.record_delivered_batch(
        workspace_id=WORKSPACE_ID,
        stream_id=stream_id,
        envelopes=[{"event_type": "e", "emitted_at": _now_iso()}],
    )


def test_deliver_does_not_fail_when_stats_redis_down(
    stream_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The buffer-writer still acks a committed batch when stats Redis is down (fail-open)."""
    import redis

    def _boom() -> Any:
        raise redis.RedisError("down")

    monkeypatch.setattr(stream_stats, "_redis", _boom)
    channel = BufferWriterChannel()
    result = channel.deliver(make_batch([make_internal_event(seq_offset=0)]))
    assert result.status == "ok"  # durability unaffected by a stats miss
