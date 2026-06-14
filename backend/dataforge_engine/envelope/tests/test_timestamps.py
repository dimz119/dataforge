"""Timestamp formatting tests (event-model §2.1 fields 12/13, §3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dataforge_engine.envelope import format_rfc3339, occurred_at_ms, to_epoch_ms
from dataforge_engine.envelope.timestamps import TimestampError


def test_six_fractional_digits_with_z() -> None:
    dt = datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=UTC)
    assert format_rfc3339(dt) == "2026-06-10T14:23:05.123456Z"


def test_zero_microseconds_padded() -> None:
    dt = datetime(2026, 5, 2, 9, 14, 33, 0, tzinfo=UTC)
    assert format_rfc3339(dt) == "2026-05-02T09:14:33.000000Z"


def test_naive_datetime_rejected() -> None:
    with pytest.raises(TimestampError):
        format_rfc3339(datetime(2026, 6, 10, 14, 23, 5, 123456))  # noqa: DTZ001


def test_non_utc_offset_normalised() -> None:
    tz = timezone(timedelta(hours=5))
    dt = datetime(2026, 6, 10, 19, 23, 5, 123456, tzinfo=tz)
    # 19:23:05+05:00 == 14:23:05Z
    assert format_rfc3339(dt) == "2026-06-10T14:23:05.123456Z"


def test_epoch_ms_truncates() -> None:
    dt = datetime(2026, 6, 10, 16, 2, 41, 9314, tzinfo=UTC)
    ms = to_epoch_ms(dt)
    # 9314 microseconds = 9.314 ms → floored to 9 ms within the second.
    assert ms % 1000 == 9
    assert occurred_at_ms(dt) == ms


def test_epoch_ms_self_consistent_with_format() -> None:
    """``to_epoch_ms`` is the millisecond truncation of the formatted instant.

    (event-model §7.2 shows an *illustrative* source.ts_ms; the doc states "Values
    are illustrative" and its literal is a day off the example's stated occurred_at,
    so we pin the library's self-consistent mapping rather than the doc literal.)
    """
    dt = datetime(2026, 6, 10, 16, 2, 41, 9314, tzinfo=UTC)
    assert to_epoch_ms(dt) == 1781107361009
    # The ms value is exactly the integer-second epoch * 1000 + truncated ms.
    assert to_epoch_ms(dt) == int(dt.timestamp()) * 1000 + 9
    assert format_rfc3339(dt) == "2026-06-10T16:02:41.009314Z"
