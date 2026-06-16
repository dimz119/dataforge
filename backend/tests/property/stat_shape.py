"""STAT-SHAPE-1/2 — the diurnal/weekly intensity-shape checks (testing-strategy § STAT).

Bucket realized ``session_started`` counts by simulated *local* hour over a multi-
simulated-day batch and compare the shape to the manifest's renormalized intensity
curve. The engine renormalizes each curve to mean 1.0, so ``target_tps`` is the
exact daily average and the curve only changes *when* arrivals land — this is the
property STAT-SHAPE proves end-to-end:

* STAT-SHAPE-1 (diurnal): each of the 8 configured buckets' realized share within
  ±10 % relative of its renormalized configured share; realized peak-to-trough ratio
  in [5.4, 6.6] (configured 6.0 ± 10 %).
* STAT-SHAPE-2 (weekly): each day's realized share within ±5 % relative; Pearson
  r ≥ 0.98 between the realized 168-hour profile and the configured
  ``diurnal x weekly`` product profile.

The instance timezone is the manifest's ``simulated_timezone`` (UTC for the builtin),
so the virtual ``occurred_at`` (RFC-3339, already in the simulated zone for UTC) maps
directly to a local hour/day. Pure stdlib — no numpy. Each check returns ``None`` on
success or a human-readable failure string (the same idiom as the PROP-RI/CDC checks).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from tests.golden.harness_full import FullBatchResult, full_ecommerce_document

_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _configured_diurnal_per_hour() -> list[float]:
    """The configured diurnal multiplier per hour (0..23), renormalized to mean 1.0."""
    doc = full_ecommerce_document()
    buckets = doc["intensity"]["diurnal"]
    per_hour = [0.0] * 24
    for b in buckets:
        for h in range(int(b["from_hour"]), int(b["to_hour"])):
            per_hour[h] = float(b["multiplier"])
    mean = sum(per_hour) / 24.0
    return [m / mean for m in per_hour]


def _configured_weekly() -> list[float]:
    """The configured weekly multiplier per dow (mon..sun), renormalized to mean 1.0."""
    doc = full_ecommerce_document()
    w = doc["intensity"]["weekly"]
    vals = [float(w[d]) for d in _DOW]
    mean = sum(vals) / 7.0
    return [v / mean for v in vals]


def _session_local(result: FullBatchResult) -> list[tuple[int, int]]:
    """(local_hour, local_dow) for every ``session_started`` (simulated_timezone=UTC)."""
    out: list[tuple[int, int]] = []
    for env in result.envelopes:
        if env["event_type"] != "session_started":
            continue
        occ = env["occurred_at"]
        dt = datetime.fromisoformat(str(occ).replace("Z", "+00:00"))
        out.append((dt.hour, dt.weekday()))
    return out


def _weekday_day_counts(result: FullBatchResult) -> list[int]:
    """How many distinct calendar days of each weekday (mon..sun) the window spans.

    A 30-sim-day window is 4 full weeks + 2 days, so two weekdays appear 5x and the
    rest 4x. The configured weekly *share* must be weighted by these occurrence
    counts (a 5x weekday carries more sessions even at equal multiplier) — otherwise
    STAT-SHAPE-2 false-fails on the window's calendar, not the curve. Computed from
    the realized session dates so it tracks the actual generated span."""
    days: set[tuple[int, int, int]] = set()
    for env in result.envelopes:
        if env["event_type"] != "session_started":
            continue
        dt = datetime.fromisoformat(str(env["occurred_at"]).replace("Z", "+00:00"))
        days.add((dt.year, dt.month, dt.day))
    counts = [0] * 7
    for y, m, d in days:
        counts[datetime(y, m, d).weekday()] += 1  # noqa: DTZ001 (weekday() is tz-independent; pure day-of-week bucketing)
    return counts


def check_shape1_diurnal(result: FullBatchResult, *, rel_tol: float = 0.10) -> str | None:
    """STAT-SHAPE-1: realized diurnal bucket shares within ±10 % rel of configured;
    peak/trough hour ratio in [5.4, 6.6]."""
    sessions = _session_local(result)
    n = len(sessions)
    if n < 5000:
        return f"STAT-SHAPE-1: only {n} sessions; need ≥5000 for a meaningful shape"
    per_hour = _configured_diurnal_per_hour()
    realized = [0] * 24
    for hour, _ in sessions:
        realized[hour] += 1
    realized_share = [c / n for c in realized]
    configured_share = [m / 24.0 for m in per_hour]  # renormalized mean 1 ⇒ share=m/24
    for h in range(24):
        exp = configured_share[h]
        got = realized_share[h]
        if exp > 0 and abs(got - exp) > rel_tol * exp:
            return (
                f"STAT-SHAPE-1: hour {h} realized share {got:.4f} != configured "
                f"{exp:.4f} (±{rel_tol:.0%} rel)"
            )
    peak = max(realized_share)
    trough = min(s for s in realized_share if s > 0)
    ratio = peak / trough if trough else float("inf")
    if not (5.4 <= ratio <= 6.6):
        return f"STAT-SHAPE-1: peak-to-trough ratio {ratio:.2f} not in [5.4, 6.6]"
    return None


def check_shape2_weekly_and_pearson(
    result: FullBatchResult, *, day_rel_tol: float = 0.05, min_r: float = 0.98
) -> str | None:
    """STAT-SHAPE-2: realized weekly day shares within ±5 % rel; Pearson r ≥ 0.98
    between the realized 168-hour profile and the configured diurnalxweekly product."""
    sessions = _session_local(result)
    n = len(sessions)
    if n < 5000:
        return f"STAT-SHAPE-2: only {n} sessions; need ≥5000 for a meaningful shape"
    weekly = _configured_weekly()
    # Weight the configured weekly share by how many of each weekday the window spans
    # (a 30-day window has 5x of two weekdays, 4x of the rest) — the realized share is
    # multiplier x occurrence-count, normalized.
    day_counts = _weekday_day_counts(result)
    weighted = [weekly[d] * day_counts[d] for d in range(7)]
    wtot = sum(weighted) or 1.0
    by_day = [0] * 7
    for _, dow in sessions:
        by_day[dow] += 1
    for d in range(7):
        exp = weighted[d] / wtot
        got = by_day[d] / n
        if exp > 0 and abs(got - exp) > day_rel_tol * exp:
            return (
                f"STAT-SHAPE-2: {_DOW[d]} realized share {got:.4f} != configured "
                f"{exp:.4f} (±{day_rel_tol:.0%} rel)"
            )
    # 168-hour profiles: configured = diurnal(hour) x weekly(dow) x occurrence-count;
    # realized = raw counts. The occurrence weight makes the configured profile match
    # the window's calendar so Pearson r reflects shape fidelity, not the span.
    diurnal = _configured_diurnal_per_hour()
    configured = [diurnal[h] * weekly[d] * day_counts[d] for d in range(7) for h in range(24)]
    realized = [0.0] * 168
    for hour, dow in sessions:
        realized[dow * 24 + hour] += 1.0
    r = _pearson(configured, realized)
    if r < min_r:
        return f"STAT-SHAPE-2: 168-hour Pearson r {r:.4f} < {min_r}"
    return None


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation of two equal-length series (pure stdlib)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    vy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (vx * vy) if vx and vy else 0.0


ALL_SHAPE_CHECKS: tuple[tuple[str, Any], ...] = (
    ("STAT-SHAPE-1", check_shape1_diurnal),
    ("STAT-SHAPE-2", check_shape2_weekly_and_pearson),
)
