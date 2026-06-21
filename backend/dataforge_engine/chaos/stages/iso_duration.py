"""ISO-8601 duration → simulated milliseconds (chaos-engine §5.6 ``params.window``).

The chaos policy carries durations as ISO-8601 strings (``PT60S``, ``PT5M``,
``PT1H30M``). The temporal stages need the simulated-millisecond width. This is a
small, total parser over the day/hour/minute/second subset the chaos config uses
(CH-V04 bounds the window to ``[PT1S, PT5M]``; the Django layer validates the
bound, this parser just converts). A malformed value yields ``0`` (the caller
treats a non-positive width as a no-op).

Pure Python (BE-ENG-1): ``re`` only.
"""

from __future__ import annotations

import re

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)

_MS_PER_SECOND = 1000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE
_MS_PER_DAY = 24 * _MS_PER_HOUR


def parse_iso_duration_ms(value: object) -> int:
    """Parse an ISO-8601 duration string into simulated milliseconds (``0`` if bad)."""
    if not isinstance(value, str):
        return 0
    match = _DURATION_RE.match(value)
    if match is None:
        return 0
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    total = (
        days * _MS_PER_DAY
        + hours * _MS_PER_HOUR
        + minutes * _MS_PER_MINUTE
        + round(seconds * _MS_PER_SECOND)
    )
    return total
