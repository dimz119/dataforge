"""``event_buffer`` page reader — the replay-stable REST page query + expiry check
(database-schema §6.1; delivery-channels §5).

This is the read side of the buffer (the write side is
:mod:`delivery.infra.buffer_store`). It executes the §6.1 normative page query —

    SELECT … FROM event_buffer
    WHERE stream_id = $1 AND (partition_ts, buffer_seq) > ($p, $s)
    ORDER BY partition_ts, buffer_seq LIMIT $page_size

— a row-comparison over the composite PK that drives partition pruning and, because
the rows are immutable, returns a **byte-identical** page for the same cursor every
time (INV-DEL-3). It also resolves the ``from=earliest|latest|<RFC3339>`` synthetic
start positions to concrete ``(p, s)`` pairs and answers the O(1) expiry question
(§5.4): a cursor whose ``p`` is past the plan retention floor, or below the oldest
attached partition's lower bound, is expired.

Reads go through the Django ``default`` connection (the ``dataforge_app``
NOBYPASSRLS role at runtime), so RLS bites: the caller arms the workspace context
(``app_workspace_id`` GUC) before reading and the scoped policy filters every row to
the armed workspace. The reader therefore needs no explicit ``workspace_id`` filter
for correctness — but the §6.1 page query keys on ``stream_id`` (the partition-prune
+ PK-order discriminator) regardless.

``partition_ts`` is stored timezone-aware; positions are exchanged as epoch
milliseconds (the cursor ``p`` unit, §5.2), converted at the SQL boundary so the
row-comparison runs against the native ``timestamptz`` column on the PK index.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.db import connection

from delivery.infra.partitions import BUFFER_TABLE

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BufferPage",
    "BufferRow",
    "earliest_position",
    "earliest_retained_position",
    "is_expired",
    "latest_position",
    "oldest_partition_floor_ms",
    "read_page",
    "retention_floor_ms",
]

_TABLE = BUFFER_TABLE

# The synthetic "before the first row" position: a cursor at (0, 0) reads from the
# very start of whatever is retained (earliest never expires while rows exist).
_ORIGIN_P = 0
_ORIGIN_S = 0


@dataclass(frozen=True)
class BufferRow:
    """One delivered row read back from ``event_buffer`` (§6.1).

    ``envelope`` is the stored **delivered shape** (the 20 contract fields,
    ``_df`` already stripped at write, BW-5) — the response payload is this object
    verbatim. ``p``/``s`` are the row's composite position, used to mint the page's
    ``next_cursor``.
    """

    p: int  # partition_ts epoch ms
    s: int  # buffer_seq
    envelope: dict[str, Any]


@dataclass(frozen=True)
class BufferPage:
    """One page of the cursor pull (§5.3).

    ``rows`` are in ``(partition_ts, buffer_seq)`` order. ``next_p``/``next_s`` is the
    position the page's ``next_cursor`` encodes — the last returned row's position,
    or the *requested* position when the page is empty (RC-2: the cursor never
    rewinds and ``next_cursor`` is never null, even at the tail).
    """

    rows: Sequence[BufferRow]
    next_p: int
    next_s: int


def _ms_to_dt(epoch_ms: int) -> datetime:
    """Epoch ms → tz-aware UTC ``datetime`` for the ``timestamptz`` comparison."""
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=UTC)


def _dt_to_ms(value: datetime) -> int:
    """tz-aware ``datetime`` → epoch ms (truncated), the cursor ``p`` unit (§5.2)."""
    return int(value.astimezone(UTC).timestamp() * 1000)


def _load_envelope(raw: Any) -> dict[str, Any]:
    """Normalize the ``envelope`` column to a dict (jsonb → dict on PG; str on SQLite)."""
    if isinstance(raw, dict):
        return raw
    import json

    return dict(json.loads(raw))


def read_page(
    *, stream_id: str, p: int, s: int, limit: int
) -> BufferPage:
    """Run the §6.1 replay-stable page query strictly after ``(p, s)`` (INV-DEL-3).

    Returns up to ``limit`` rows in composite-PK order. The workspace context must
    already be armed (RLS scopes the read). Re-running with the same ``(p, s, limit)``
    over the immutable buffer returns an identical page — the replay contract.

    The cursor ``p`` is the row's ``partition_ts`` *truncated to milliseconds* (the
    §5.2 unit), but ``partition_ts`` itself is stored at sub-millisecond precision.
    A naive ``(partition_ts, buffer_seq) > (ms_to_dt(p), s)`` would therefore re-emit
    a row in the same millisecond (its ``.xxxµs`` > the truncated boundary). The
    boundary is expressed against the half-open millisecond window instead, exactly
    matching the position the page minted: a row qualifies iff it lands in a strictly
    *later* millisecond, or in the *same* millisecond with a higher ``buffer_seq``
    (``buffer_seq`` is the strictly-monotonic, order-equivalent tiebreaker, §6.1).
    Both arms range over the native ``timestamptz`` column on the PK index.
    """
    sql = (
        f"SELECT partition_ts, buffer_seq, envelope FROM {_TABLE} "
        f"WHERE stream_id = %s AND ("
        f"  partition_ts >= %s "  # row in a strictly later millisecond
        f"  OR (partition_ts >= %s AND partition_ts < %s AND buffer_seq > %s)"  # same ms
        f") ORDER BY partition_ts, buffer_seq LIMIT %s"
    )
    sid = _uuid_param(stream_id)
    ms_floor = _ms_to_dt(p)  # start of the cursor's millisecond
    ms_next = _ms_to_dt(p + 1)  # start of the next millisecond
    rows: list[BufferRow] = []
    with connection.cursor() as cursor:
        cursor.execute(sql, [sid, ms_next, ms_floor, ms_next, s, limit])
        for partition_ts, buffer_seq, envelope in cursor.fetchall():
            row_p = _dt_to_ms(_as_dt(partition_ts))
            rows.append(
                BufferRow(p=row_p, s=int(buffer_seq), envelope=_load_envelope(envelope))
            )
    if rows:
        last = rows[-1]
        return BufferPage(rows=rows, next_p=last.p, next_s=last.s)
    # Empty page at the tail: the cursor does not move (RC-2/RC-3).
    return BufferPage(rows=rows, next_p=p, next_s=s)


def earliest_position() -> tuple[int, int]:
    """The ``from=earliest`` synthetic: the origin position (before any row, §6.1).

    A page query from ``(0, 0)`` reads the oldest retained rows first; the origin is
    never expired while any row remains (its ``p`` is 0, below every floor, but
    earliest is the recovery target itself — the API resolves ``from`` to a position
    and skips the expiry check on the synthetic start).
    """
    return (_ORIGIN_P, _ORIGIN_S)


def earliest_retained_position(*, stream_id: str) -> tuple[int, int]:
    """The recovery position for the ``earliest_cursor`` of a 410 (§5.4).

    Unlike :func:`earliest_position` (the synthetic origin ``(0, 0)`` a fresh
    ``from=earliest`` resolves to), this returns the position **just before** the
    oldest currently-retained row, so the minted ``earliest_cursor`` token decodes to
    a position *above* the expiry floor — re-presenting it does not 410 again (the
    spec's "recovery is one request away"). Empty stream → the origin.
    """
    sql = (
        f"SELECT partition_ts, buffer_seq FROM {_TABLE} "
        f"WHERE stream_id = %s ORDER BY partition_ts, buffer_seq LIMIT 1"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [_uuid_param(stream_id)])
        row = cursor.fetchone()
    if row is None:
        return earliest_position()
    oldest_p = _dt_to_ms(_as_dt(row[0]))
    oldest_s = int(row[1])
    # Exclusive: a cursor at (oldest_p, oldest_s - 1) yields the oldest row first.
    # The position stays within the same millisecond, so it is above the floor.
    return (oldest_p, max(_ORIGIN_S, oldest_s - 1))


def latest_position(*, stream_id: str) -> tuple[int, int]:
    """The ``from=latest`` synthetic: the current tail (after the last row, §6.1).

    Resolves to the max ``(partition_ts, buffer_seq)`` for the stream so the first
    page is empty and the consumer tails new events. Empty stream → the origin.
    """
    sql = (
        f"SELECT partition_ts, buffer_seq FROM {_TABLE} "
        f"WHERE stream_id = %s ORDER BY partition_ts DESC, buffer_seq DESC LIMIT 1"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [_uuid_param(stream_id)])
        row = cursor.fetchone()
    if row is None:
        return earliest_position()
    return (_dt_to_ms(_as_dt(row[0])), int(row[1]))


def from_wall_time_position(*, stream_id: str, when: datetime) -> tuple[int, int]:
    """The ``from=<RFC3339>`` synthetic: the first row with ``emitted_at`` ≥ ``when``.

    Resolves to the position *before* that row (exclusive), so the first page begins
    at the requested wall time (§5.1). No matching row → the tail (nothing to read
    yet). The position is one buffer_seq below the matched row to keep the cursor
    exclusive.
    """
    sql = (
        f"SELECT partition_ts, buffer_seq FROM {_TABLE} "
        f"WHERE stream_id = %s AND emitted_at >= %s "
        f"ORDER BY partition_ts, buffer_seq LIMIT 1"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [_uuid_param(stream_id), when.astimezone(UTC)])
        row = cursor.fetchone()
    if row is None:
        return latest_position(stream_id=stream_id)
    matched_p = _dt_to_ms(_as_dt(row[0]))
    matched_s = int(row[1])
    # Exclusive: a cursor at (matched_p, matched_s - 1) yields the matched row first.
    return (matched_p, max(_ORIGIN_S, matched_s - 1))


def retention_floor_ms(*, now: datetime, retention_hours: int) -> int:
    """The logical retention floor in epoch ms (§4.3 / §5.4): ``now - retention``.

    A cursor whose ``p`` is strictly below this is expired by the plan's logical
    window (24 h Free / 48 h paid), even if the physical partition still exists.
    """
    floor = now.astimezone(UTC) - timedelta(hours=retention_hours)
    return _dt_to_ms(floor)


def oldest_partition_floor_ms() -> int | None:
    """The lower bound of the oldest attached ``event_buffer`` partition, in ms (§5.4).

    A cursor whose ``p`` precedes this physical floor points into a dropped
    partition → expired. ``None`` when no partitions are attached (a fresh DB or the
    SQLite unit lane with no partition machinery — the caller then relies on the
    logical floor alone).
    """
    # Postgres-only: read the attached partitions' RANGE lower bounds from the
    # catalog. On SQLite (no partitioning) there is no physical floor.
    if connection.vendor != "postgresql":
        return None
    sql = """
        SELECT min(
            (regexp_match(
                pg_get_expr(c.relpartbound, c.oid),
                'FROM \\(''([^'']+)'''))[1]::timestamptz
        )
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = %s
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [_TABLE])
        row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return _dt_to_ms(_as_dt(row[0]))


def is_expired(*, cursor_p: int, retention_floor: int, physical_floor: int | None) -> bool:
    """The §5.4 expiry predicate, checked before any query (RC-9/RC-10).

    Expired iff ``cursor_p`` is below the logical retention floor **or** below the
    oldest attached partition's physical floor. Both are O(1) ``p`` comparisons.
    """
    if cursor_p < retention_floor:
        return True
    return physical_floor is not None and cursor_p < physical_floor


def _as_dt(value: Any) -> datetime:
    """Coerce a DB partition_ts value to a tz-aware UTC datetime (PG / SQLite)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    # SQLite returns ISO strings for timestamptz columns under raw SQL.
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _uuid_param(value: str) -> str:
    """Format a UUID for the active DB's raw-SQL binding (dashed PG / 32-hex SQLite)."""
    import uuid

    parsed = uuid.UUID(value)
    return str(parsed) if connection.vendor == "postgresql" else parsed.hex
