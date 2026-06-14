"""RFC 3339 timestamp formatting for the envelope (event-model §2.1 fields 12/13).

``occurred_at`` and ``emitted_at`` are pinned to RFC 3339 UTC with **exactly 6
fractional digits** and a literal ``Z`` suffix (no ``+00:00``). The simulated
clock is millisecond-resolution at the boundary (``event_id`` timestamp bits and
``cdc.source.ts_ms`` are ``occurred_at`` *milliseconds*), but the wire format
always carries microsecond precision (6 digits), zero-padded.

This module owns the two directions the envelope needs:

* :func:`format_rfc3339` — a timezone-aware :class:`datetime` → the canonical
  string (6 fractional digits, ``Z``).
* :func:`to_epoch_ms` / :func:`occurred_at_ms` — a canonical instant → epoch
  milliseconds (the value the UUIDv7 timestamp bits and the CDC ``ts_ms`` /
  ``source.ts_ms`` fields use).

Pure Python; ``datetime`` only (BE-ENG-1). All inputs must be tz-aware UTC —
naive datetimes are rejected (DTZ discipline; ruff DTZ rules are on).
"""

from __future__ import annotations

from datetime import UTC, datetime

# Milliseconds since the Unix epoch fit in 48 bits well past year 10000, so the
# UUIDv7 timestamp field (event-model §2.2.1) never overflows for simulated time.
_MS_PER_SECOND = 1000


class TimestampError(ValueError):
    """Raised for a non-UTC / naive datetime where a canonical instant is required."""


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError("timestamp must be timezone-aware (UTC) — naive datetime rejected")
    if value.utcoffset() != UTC.utcoffset(None):
        # Normalise any UTC-equivalent offset (e.g. +00:00) to canonical UTC.
        return value.astimezone(UTC)
    return value


def format_rfc3339(value: datetime) -> str:
    """Format a tz-aware UTC instant as RFC 3339 with 6 fractional digits and ``Z``.

    e.g. ``datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=utc)`` →
    ``"2026-06-10T14:23:05.123456Z"``. Sub-microsecond input is impossible
    (``datetime`` resolution is microseconds), so the 6-digit field is exact.
    """
    utc = _require_utc(value)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond:06d}Z"


def to_epoch_ms(value: datetime) -> int:
    """A tz-aware UTC instant → integer epoch milliseconds (truncated, not rounded).

    Truncation (floor toward the epoch for the fractional millisecond) keeps the
    mapping ``occurred_at`` → ``event_id`` timestamp bits stable: the same
    canonical instant always yields the same ms, so ``event_id`` stays
    deterministic (INV-GEN-3).
    """
    utc = _require_utc(value)
    return int(utc.timestamp() * _MS_PER_SECOND)


def occurred_at_ms(occurred_at: datetime) -> int:
    """Milliseconds of ``occurred_at`` — the UUIDv7 timestamp bits and the CDC
    ``source.ts_ms`` value (event-model §2.2.1, §4.2). Alias for clarity at call
    sites that mean "simulated change time in ms".
    """
    return to_epoch_ms(occurred_at)


def emitted_at_ms(emitted_at: datetime) -> int:
    """Milliseconds of ``emitted_at`` — the CDC top-level ``ts_ms`` value
    (event-model §4.2). Alias for clarity at call sites that mean "wall-clock
    processing time in ms".
    """
    return to_epoch_ms(emitted_at)
