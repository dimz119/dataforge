"""Postgres-side reads over ``event_buffer`` for StreamStats (observability §5).

Two read shapes the stats surface needs from the durable buffer (the rebuild source
of truth, INV-OBS-2):

* :func:`read_buffer_window` — the oldest/newest retained ``emitted_at`` for one
  stream, for the ``buffer`` block of the §4.11.1 response (``earliest_available_at``
  / ``latest_event_at``);
* :func:`tally_from_buffer` — the full ``total_events`` / ``by_event_type`` /
  ``last_event_at`` reconstruction for ``manage.py rebuild_stream_stats`` (the Redis
  loss recovery path), plus the recent ``emitted_at`` ms for repopulating the
  observed_tps ring.

Reads go through the Django ``default`` connection; the caller arms the workspace
context first so RLS scopes ``event_buffer`` to the owning tenant (INV-OBS-3) — the
same scoping the REST cursor read uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django.db import connection

from delivery.infra.partitions import PHYSICAL_RETENTION_HOURS

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BufferTally",
    "BufferWindow",
    "read_buffer_window",
    "retention_hours",
    "tally_from_buffer",
]

_TABLE = "event_buffer"


def retention_hours() -> int:
    """The buffer retention window in hours (delivery-channels §4.3; §4.11.1)."""
    return PHYSICAL_RETENTION_HOURS


def _uuid_param(value: str) -> str:
    """Format a UUID for the active DB's raw-SQL binding (dashed PG / 32-hex SQLite)."""
    parsed = uuid.UUID(value)
    return str(parsed) if connection.vendor == "postgresql" else parsed.hex


def _as_dt(value: object) -> datetime:
    """Normalise a DB timestamp (str on SQLite, datetime on PG) to tz-aware UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _format_rfc3339(value: datetime) -> str:
    """RFC-3339 UTC with 6 fractional digits + ``Z`` (matches the envelope wall format)."""
    utc = value.astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond:06d}Z"


@dataclass(frozen=True)
class BufferWindow:
    """The retained ``emitted_at`` span for one stream (``None`` for an empty buffer)."""

    earliest_available_at: str | None
    latest_event_at: str | None
    retention_hours: int


def read_buffer_window(*, stream_id: str) -> BufferWindow:
    """Oldest + newest retained ``emitted_at`` for the ``buffer`` block (§4.11.1).

    One aggregate query (``min``/``max`` over the stream's rows); both ``None`` when
    the buffer holds no rows yet. The workspace must be armed by the caller (RLS).
    """
    sql = f"SELECT min(emitted_at), max(emitted_at) FROM {_TABLE} WHERE stream_id = %s"
    with connection.cursor() as cursor:
        cursor.execute(sql, [_uuid_param(stream_id)])
        row = cursor.fetchone()
    earliest = _format_rfc3339(_as_dt(row[0])) if row and row[0] is not None else None
    latest = _format_rfc3339(_as_dt(row[1])) if row and row[1] is not None else None
    return BufferWindow(
        earliest_available_at=earliest,
        latest_event_at=latest,
        retention_hours=retention_hours(),
    )


@dataclass(frozen=True)
class BufferTally:
    """A full StreamStats reconstruction from ``event_buffer`` (the rebuild source)."""

    total_events: int
    by_event_type: dict[str, int]
    last_event_at: str | None
    recent_emitted_ms: Sequence[int]


def tally_from_buffer(*, stream_id: str, tps_window_s: int) -> BufferTally:
    """Recompute total / per-type / last_event_at from ``event_buffer`` (INV-OBS-2).

    The rebuild source of truth: counters are derivable from the buffer, so a Redis
    loss is recoverable. One grouped count for ``by_event_type`` + total, one ``max``
    for ``last_event_at``, and the recent ``emitted_at`` ms (within ``tps_window_s``
    of the tail) so ``observed_tps`` recovers too. Workspace armed by the caller (RLS).
    """
    sid = _uuid_param(stream_id)
    group_sql = (
        f"SELECT event_type, count(*) FROM {_TABLE} "
        f"WHERE stream_id = %s GROUP BY event_type"
    )
    with connection.cursor() as cursor:
        cursor.execute(group_sql, [sid])
        rows = cursor.fetchall()
    by_event_type = {str(event_type): int(count) for event_type, count in rows}
    total = sum(by_event_type.values())

    max_sql = f"SELECT max(emitted_at) FROM {_TABLE} WHERE stream_id = %s"
    with connection.cursor() as cursor:
        cursor.execute(max_sql, [sid])
        max_row = cursor.fetchone()
    if not max_row or max_row[0] is None:
        return BufferTally(
            total_events=0, by_event_type={}, last_event_at=None, recent_emitted_ms=[]
        )
    tail = _as_dt(max_row[0])
    last_event_at = _format_rfc3339(tail)

    floor = tail - timedelta(seconds=tps_window_s)
    recent_sql = (
        f"SELECT emitted_at FROM {_TABLE} "
        f"WHERE stream_id = %s AND emitted_at >= %s"
    )
    with connection.cursor() as cursor:
        cursor.execute(recent_sql, [sid, floor])
        recent = cursor.fetchall()
    recent_ms = [int(_as_dt(r[0]).timestamp() * 1000) for r in recent]
    return BufferTally(
        total_events=total,
        by_event_type=by_event_type,
        last_event_at=last_event_at,
        recent_emitted_ms=recent_ms,
    )
