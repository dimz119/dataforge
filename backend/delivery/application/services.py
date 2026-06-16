"""Use-case service for the REST cursor pull (delivery-channels §5).

The single read use case behind ``GET /api/v1/streams/{id}/events``: given a stream,
a start spec (``from`` or ``cursor``), an optional ``types`` filter, and a page
``limit``, it resolves the start position, enforces the §5.4 expiry contract, runs
the §6.1 replay-stable page query, applies the (cursor-unrenumbering, RC-4) filter,
and returns the page plus the opaque ``next_cursor``.

It owns two cursor-binding concerns the codec is deliberately agnostic about:

* **canonicalization of the filter set** — the fingerprint binds a cursor to its
  filter set (RC-7), so equivalent filter inputs must canonicalize to one string.
  ``types`` canonicalizes to a sorted, comma-joined list (RC-4: a cursor presented
  with a different filter set fails ``cursor-invalid``).
* **the retention window** — read from the workspace's quota
  (``buffer_retention_hours``, 24 Free / 48 paid), so the logical-expiry floor is the
  caller's plan window (§4.3 / §5.4).

The service is framework-light beyond Django ORM/DB access (it reads the quota row
and runs the page query); HTTP shaping (auth, masking, problem rendering) is the
viewset's job. Errors are raised as domain exceptions the viewset maps to the RFC
9457 problem types (``cursor-invalid`` 400, ``cursor-expired`` 410).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from delivery.domain import event_filter
from delivery.domain.cursor import (
    CursorDecodeError,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)
from delivery.infra import buffer_reader

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CursorExpiredError",
    "CursorInvalidError",
    "EventsPage",
    "EventsQuery",
    "read_events",
]

# The default plan retention if no quota row exists (Free tier, §4.3).
_DEFAULT_RETENTION_HOURS = 24


class CursorInvalidError(Exception):
    """An undecodable cursor or one bound to a different stream/filter set (RC-8).

    Mapped by the viewset to ``400 cursor-invalid``.
    """


class CursorExpiredError(Exception):
    """A cursor past the retention window or into a dropped partition (§5.4).

    Carries the ``earliest_cursor`` (the recovery position) and ``retention_hours``
    so the viewset can render the §5.4 ``410 cursor-expired`` body verbatim.
    """

    def __init__(self, *, earliest_cursor: str, retention_hours: int) -> None:
        super().__init__("cursor expired")
        self.earliest_cursor = earliest_cursor
        self.retention_hours = retention_hours


@dataclass(frozen=True)
class EventsQuery:
    """The validated, normalized inputs to one page read (§5.1).

    Exactly one of ``cursor`` / ``from_spec`` is the start. ``types`` is the parsed
    filter (already trimmed of empties; ≤ 20 entries enforced by the serializer).
    ``limit`` is clamped to 1..1000 by the serializer.
    """

    stream_id: str
    limit: int
    cursor: str | None = None
    from_spec: str | None = None  # "earliest" | "latest" | RFC-3339
    types: tuple[str, ...] = ()
    # Phase 8 per-entity CDC filter (R-CDC-7): both or neither; matched against
    # ``entity_refs`` with IDENTICAL semantics to the WS auth frame.
    entity_type: str | None = None
    entity_key: str | None = None


@dataclass(frozen=True)
class EventsPage:
    """One rendered page: the delivered envelopes + the opaque ``next_cursor``.

    ``next_cursor`` is never ``None`` (RC-2): a full page means poll again, an empty
    page means the tail was reached — the cursor stays put and the consumer re-polls.
    """

    data: Sequence[dict[str, Any]]
    next_cursor: str


def canonical_filter_set(
    types: Sequence[str],
    *,
    entity_type: str | None = None,
    entity_key: str | None = None,
) -> str:
    """The canonical filter-set string the fingerprint binds to (RC-4/RC-7).

    ``types`` → a sorted, comma-joined list (deduplicated); empty → ``""``. Sorting
    + dedup makes ``?types=b,a`` and ``?types=a,b,a`` the same filter set ⇒ one
    fingerprint ⇒ interchangeable cursors over an identical filter.

    Phase 8: the per-entity CDC filter (``entity_type``/``entity_key``, R-CDC-7) is
    part of the filter set too (P-3) — appended as ``e:{type}:{key}`` so a cursor
    minted under an entity filter only replays under the SAME one. Restated
    identically in :mod:`delivery.domain.ws_cursor` so REST + WS share one fingerprint.
    """
    cleaned = sorted({t for t in types if t})
    base = ",".join(cleaned)
    if entity_type and entity_key:
        return f"{base}|e:{entity_type}:{entity_key}"
    return base


def read_events(query: EventsQuery, *, now: datetime | None = None) -> EventsPage:
    """Execute one cursor-pull page (§5.1-5.4). Workspace context must be armed.

    Resolves the start position (``cursor`` → decode+verify+expiry; ``from`` →
    synthetic), runs the §6.1 page query over the *unfiltered* stream, applies the
    ``types`` filter without renumbering the cursor (RC-4), and mints ``next_cursor``
    bound to this stream + filter set.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    filter_set = canonical_filter_set(
        query.types, entity_type=query.entity_type, entity_key=query.entity_key
    )
    fingerprint = filter_fingerprint(
        stream_id=query.stream_id, canonical_filter_set=filter_set
    )
    retention_hours = _retention_hours_for_current_workspace()

    start_p, start_s = _resolve_start(
        query=query,
        fingerprint=fingerprint,
        retention_hours=retention_hours,
        now=moment,
    )

    # The §6.1 replay-stable page query over the UNFILTERED stream (RC-4: filters
    # narrow delivery, never renumber it — the cursor advances over every row).
    page = buffer_reader.read_page(
        stream_id=query.stream_id, p=start_p, s=start_s, limit=query.limit
    )

    wanted = set(query.types)
    data: list[dict[str, Any]] = []
    for row in page.rows:
        # The shared R-CDC-7 predicate (types ∧ per-entity), identical on REST + WS;
        # filtered-out rows are skipped server-side but the cursor still advances (RC-4).
        if not event_filter.envelope_matches(
            row.envelope,
            types=wanted,
            entity_type=query.entity_type,
            entity_key=query.entity_key,
        ):
            continue
        data.append(row.envelope)

    next_cursor = encode_cursor(p=page.next_p, s=page.next_s, fingerprint=fingerprint)
    return EventsPage(data=data, next_cursor=next_cursor)


def _resolve_start(
    *,
    query: EventsQuery,
    fingerprint: str,
    retention_hours: int,
    now: datetime,
) -> tuple[int, int]:
    """Resolve the (exclusive) start position from ``cursor`` or ``from`` (§5.1-5.4)."""
    if query.cursor is not None:
        try:
            position = decode_cursor(query.cursor, expected_fingerprint=fingerprint)
        except CursorDecodeError as exc:
            raise CursorInvalidError(str(exc)) from exc
        _enforce_not_expired(
            cursor_p=position.p,
            stream_id=query.stream_id,
            fingerprint=fingerprint,
            retention_hours=retention_hours,
            now=now,
        )
        return (position.p, position.s)

    spec = (query.from_spec or "earliest").strip()
    if spec == "earliest":
        return buffer_reader.earliest_position()
    if spec == "latest":
        return buffer_reader.latest_position(stream_id=query.stream_id)
    # RFC-3339 wall time: first buffer position with emitted_at ≥ the given instant.
    when = _parse_rfc3339(spec)
    return buffer_reader.from_wall_time_position(stream_id=query.stream_id, when=when)


def _enforce_not_expired(
    *, cursor_p: int, stream_id: str, fingerprint: str, retention_hours: int, now: datetime
) -> None:
    """The §5.4 expiry gate, checked before any page query (RC-9/RC-10).

    Raises :class:`CursorExpiredError` carrying the ``earliest_cursor`` — the oldest
    retained position, fingerprint-bound to the SAME stream + filter set, so the
    client resumes the identical pagination one request later (§5.4) — when the
    cursor is past the logical floor or into a dropped partition. The earliest cursor
    points just before the oldest retained row, so re-presenting it does not 410.
    """
    retention_floor = buffer_reader.retention_floor_ms(
        now=now, retention_hours=retention_hours
    )
    physical_floor = buffer_reader.oldest_partition_floor_ms()
    if buffer_reader.is_expired(
        cursor_p=cursor_p, retention_floor=retention_floor, physical_floor=physical_floor
    ):
        earliest_p, earliest_s = buffer_reader.earliest_retained_position(
            stream_id=stream_id
        )
        earliest_cursor = encode_cursor(
            p=earliest_p, s=earliest_s, fingerprint=fingerprint
        )
        raise CursorExpiredError(
            earliest_cursor=earliest_cursor, retention_hours=retention_hours
        )


# -- internal helpers -------------------------------------------------------


def _parse_rfc3339(value: str) -> datetime:
    """Parse an RFC-3339 ``from`` value to a tz-aware UTC datetime (else invalid)."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CursorInvalidError("Invalid 'from' wall-time value.") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _retention_hours_for_current_workspace() -> int:
    """The armed workspace's ``buffer_retention_hours`` quota (24 Free / 48 paid).

    Read through the scoped manager (the context is armed); a missing quota row
    falls back to the Free default. Plan retention is the logical expiry window.
    """
    from tenancy.domain.models import WorkspaceQuotas

    quota = WorkspaceQuotas.objects.first()
    if quota is None:
        return _DEFAULT_RETENTION_HOURS
    return int(quota.buffer_retention_hours)
