"""The Phase-6 stream-control OPS/XCH/SOAK harness (testing-strategy §11, §8.3, §13).

The companion to :mod:`tests.ops.failover_harness`: the live round-trips it powers
(OPS-5 dynamic-TPS stopwatch, OPS-8 WS backpressure, the XCH cross-channel content
seam, the SOAK-200 RSS/lag profile) are **compose-only** — they need Kafka + the
``ws`` ASGI process + a Redis channel layer (the verify agent's ``demo-phase06.sh``
and the nightly soak lane). But the *pass/fail logic* those runs lean on is pure and
MUST be CI-gated, so a regression in the assertion math cannot let a real violation
pass silently. This module is that logic — observation-window math, drop-notice
reconciliation, content-equality, and the soak regression thresholds — and the unit
suite (:mod:`tests.ops.test_stream_control_harness`) pins it.

All functions are pure: they take primitive observations (timestamps, counts, parsed
event dicts) and return a verdict or raise :class:`AssertionError` with a message
naming the breach. No Django, no Redis, no broker.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# -- Budgets (phase-06 exit criteria; testing-strategy §11, §13.1) ------------
TPS_EFFECTIVE_BUDGET_S = 2.0  # OPS-5: 10→500 effective within ≤ 2 s
TPS_EFFECTIVE_TOLERANCE = 0.20  # observed within 20 % of the new target counts as reached
SOAK_RSS_SLOPE_MAX_MIB_PER_MIN = 1.0  # SOAK-200: < 1 MiB/min RSS slope
SOAK_RSS_GROWTH_MAX_FRAC = 0.10  # and < 10 % total growth
SOAK_LAG_P99_MAX_S = 5.0  # consumer-lag p99 < 5 s
SOAK_STALENESS_MAX_S = 5.0  # stats staleness ≤ 5 s throughout (INV-OBS-2)


# ---------------------------------------------------------------------------
# OPS-5 — dynamic TPS effectiveness stopwatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TpsObservation:
    """One observed-rate sample: ``observed_tps`` measured ``t_s`` seconds after ack."""

    t_s: float
    observed_tps: float


def observed_rate_from_emitted_at(emitted_at_ms: Sequence[int], window_s: float = 1.0) -> float:
    """The realized inter-event rate over the LAST ``window_s`` of a delivered run.

    The OPS-5 measurement primitive (testing-strategy §11: "measured by observed
    inter-event rate"): given sorted ``emitted_at`` epoch-ms, count events whose
    ``emitted_at`` falls within ``window_s`` of the most recent one and divide by the
    window. Robust to warm-up: only the trailing window is rated."""
    if len(emitted_at_ms) < 2:
        return 0.0
    last = emitted_at_ms[-1]
    floor = last - int(window_s * 1000)
    in_window = sum(1 for ms in emitted_at_ms if ms >= floor)
    return in_window / window_s if window_s > 0 else 0.0


def time_to_reach_target(
    samples: Sequence[TpsObservation],
    *,
    target_tps: float,
    tolerance: float = TPS_EFFECTIVE_TOLERANCE,
) -> float | None:
    """The first sample time (s after ack) at which ``observed_tps`` reached target.

    "Reached" = within ``tolerance`` below the target (overshoot always counts). The
    stopwatch the OPS-5 demo runs once per second after the PATCH ack; ``None`` means
    the target was never reached within the sampled span."""
    floor = target_tps * (1.0 - tolerance)
    for s in sorted(samples, key=lambda o: o.t_s):
        if s.observed_tps >= floor:
            return s.t_s
    return None


def assert_tps_effective_within_budget(
    samples: Sequence[TpsObservation],
    *,
    target_tps: float,
    budget_s: float = TPS_EFFECTIVE_BUDGET_S,
    tolerance: float = TPS_EFFECTIVE_TOLERANCE,
) -> float:
    """OPS-5: assert the new ``target_tps`` is effective within ``budget_s``.

    Returns the measured time-to-effect. Raises with the breach detail otherwise —
    either the target was never reached, or it was reached too late."""
    reached = time_to_reach_target(samples, target_tps=target_tps, tolerance=tolerance)
    if reached is None:
        best = max((o.observed_tps for o in samples), default=0.0)
        raise AssertionError(
            f"OPS-5: observed_tps never reached {target_tps} "
            f"(within {tolerance:.0%}); best sample was {best:.1f} tps over "
            f"{len(samples)} samples"
        )
    if reached > budget_s:
        raise AssertionError(
            f"OPS-5: TPS change took {reached:.2f} s to take effect, "
            f"exceeding the {budget_s} s budget (phase-06 exit #3)"
        )
    return reached


# ---------------------------------------------------------------------------
# OPS-8 — WS drop-oldest backpressure reconciliation
# ---------------------------------------------------------------------------


def assert_drop_counts_reconcile(
    *,
    produced: int,
    delivered: int,
    drop_notice_total: int,
) -> None:
    """OPS-8 / INV-DEL-5: every produced frame is either delivered or accounted for
    by a drop-notice count — exactly, no slack.

    ``produced == delivered + drop_notice_total``. A surplus means frames vanished
    without a drop-notice (a silent loss — the cardinal WS-backpressure failure); a
    deficit means a drop-notice over-counted (a spurious gap report)."""
    accounted = delivered + drop_notice_total
    if accounted != produced:
        delta = produced - accounted
        kind = "silent loss" if delta > 0 else "over-counted drop-notice"
        raise AssertionError(
            f"OPS-8: drop reconciliation failed ({kind}): produced={produced} "
            f"delivered={delivered} dropped(reported)={drop_notice_total} "
            f"→ unaccounted={delta} (INV-DEL-5 requires exact reconciliation)"
        )


def assert_memory_bounded(*, queue_len: int, capacity: int) -> None:
    """OPS-8: the per-connection send queue never exceeds its cap (bounded memory)."""
    if queue_len > capacity:
        raise AssertionError(
            f"OPS-8: send queue grew to {queue_len} > cap {capacity} — "
            "drop-oldest backpressure is not bounding memory (INV-DEL-5)"
        )


# ---------------------------------------------------------------------------
# XCH — cross-channel content equality (WS == REST over a shared window)
# ---------------------------------------------------------------------------


def _content_key(event: Mapping[str, Any]) -> str:
    """A wire-order-independent canonical content key for one delivered event (S-3).

    JSON with sorted keys at every level, ``_df`` excluded (it is never delivered —
    a leak is the SB-3 suite's job, not XCH's). Two channels deliver the *same*
    event iff this projection is byte-equal, regardless of wire key order (S-3)."""
    delivered = {k: v for k, v in event.items() if not k.startswith("_df")}
    return json.dumps(delivered, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class XchReport:
    """The verdict of a cross-channel content comparison over a shared window."""

    rest_ids: set[str]
    ws_ids: set[str]
    content_mismatches: list[str] = field(default_factory=list)
    partition_order_ok: bool = True

    @property
    def common_ids(self) -> set[str]:
        return self.rest_ids & self.ws_ids

    @property
    def ws_only(self) -> set[str]:
        return self.ws_ids - self.rest_ids

    @property
    def rest_only(self) -> set[str]:
        return self.rest_ids - self.ws_ids


def compare_channels(
    rest_events: Sequence[Mapping[str, Any]],
    ws_events: Sequence[Mapping[str, Any]],
) -> XchReport:
    """Build the XCH content report: per-``event_id`` sets, content-equality on the
    intersection, and per-``partition_key`` relative order on the WS subset (S-3).

    REST is the authoritative complete record (the buffer never drops); WS is an
    at-most-once tail (a subset is allowed — XCH-2). This compares *content*, leaving
    the drop reconciliation to :func:`assert_xch`."""
    rest_by_id = {str(e["event_id"]): e for e in rest_events}
    ws_by_id = {str(e["event_id"]): e for e in ws_events}
    mismatches: list[str] = []
    for eid in rest_by_id.keys() & ws_by_id.keys():
        if _content_key(rest_by_id[eid]) != _content_key(ws_by_id[eid]):
            mismatches.append(eid)

    # Per-partition relative order: the WS-delivered events of each partition_key must
    # appear in the same relative order as in REST (no reordering within a key).
    rest_order = {pk: i for i, pk in enumerate(str(e["event_id"]) for e in rest_events)}
    order_ok = True
    by_pk: dict[str, list[str]] = {}
    for e in ws_events:
        eid = str(e["event_id"])
        if eid in rest_by_id:
            by_pk.setdefault(str(e.get("partition_key", "")), []).append(eid)
    for ids in by_pk.values():
        positions = [rest_order[i] for i in ids if i in rest_order]
        if positions != sorted(positions):
            order_ok = False
            break

    return XchReport(
        rest_ids=set(rest_by_id),
        ws_ids=set(ws_by_id),
        content_mismatches=mismatches,
        partition_order_ok=order_ok,
    )


def assert_xch(
    report: XchReport,
    *,
    ws_drop_notice_total: int,
    clean: bool,
) -> None:
    """Assert the XCH-1/2 contract over a comparison report.

    XCH-1 (clean): identical ``event_id`` sets, zero drop notices, per-key order
    preserved, content-equal on every event. XCH-2 (chaos): WS may be a *subset*
    (at-most-once), but the drop-notice count must reconcile the difference exactly
    (INV-DEL-5) — ``|rest_only| == drop_notice_total`` — and every common event must
    still be content-equal."""
    if report.content_mismatches:
        raise AssertionError(
            f"XCH: {len(report.content_mismatches)} event(s) differ in content "
            f"between WS and REST (e.g. {report.content_mismatches[0]}); per-event "
            "content must be identical across channels (S-3)"
        )
    if report.ws_only:
        raise AssertionError(
            f"XCH: {len(report.ws_only)} event(s) on WS not in REST "
            f"(e.g. {next(iter(report.ws_only))}) — REST is the complete record; "
            "WS must never deliver an event the buffer lacks"
        )
    if not report.partition_order_ok:
        raise AssertionError("XCH: WS reordered events within a partition_key (S-3 FIFO)")
    if clean:
        if report.rest_only:
            raise AssertionError(
                f"XCH-1 (clean): WS missing {len(report.rest_only)} REST event(s) at a "
                "rate that should have zero drops — backpressure should not fire here"
            )
        if ws_drop_notice_total != 0:
            raise AssertionError(
                f"XCH-1 (clean): {ws_drop_notice_total} drop notice(s) at a no-drop rate"
            )
    else:
        # XCH-2: the subset gap must be exactly the reported drops (INV-DEL-5).
        gap = len(report.rest_only)
        if gap != ws_drop_notice_total:
            raise AssertionError(
                f"XCH-2 (chaos): WS missed {gap} event(s) but drop notices reported "
                f"{ws_drop_notice_total} — the counts must reconcile exactly (INV-DEL-5)"
            )


# ---------------------------------------------------------------------------
# SOAK-200 — RSS-slope / lag / staleness / tally regression analysis (§13.1)
# ---------------------------------------------------------------------------


def linear_regression_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Ordinary-least-squares slope of ``ys`` vs ``xs`` (units: y-per-x).

    The SOAK-200 trend primitive: RSS-vs-minute slope and lag-vs-time slope both
    reduce to this. Returns 0.0 for a degenerate (constant-x / single-point) series."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0.0:
        return 0.0
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return num / denom


def percentile(values: Sequence[float], p: float) -> float:
    """The ``p``-quantile (0..1) via nearest-rank on the sorted sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0, min(len(ordered) - 1, round(p * (len(ordered) - 1))))
    return ordered[rank]


@dataclass(frozen=True)
class RssSample:
    """One RSS measurement: ``minute`` since warm-up end, ``rss_mib`` resident set."""

    minute: float
    rss_mib: float


def assert_rss_stable(samples: Sequence[RssSample]) -> tuple[float, float]:
    """SOAK-200: RSS slope < 1 MiB/min AND total growth < 10 % over the window.

    Returns ``(slope_mib_per_min, growth_fraction)``. Raises on either breach — a
    positive slope (a leak) or a large total growth even at a flat slope."""
    if len(samples) < 2:
        raise AssertionError("SOAK-200: need ≥ 2 RSS samples to fit a slope")
    xs = [s.minute for s in samples]
    ys = [s.rss_mib for s in samples]
    slope = linear_regression_slope(xs, ys)
    first = ys[0] or 1e-9
    growth = (max(ys) - ys[0]) / first
    if slope >= SOAK_RSS_SLOPE_MAX_MIB_PER_MIN:
        raise AssertionError(
            f"SOAK-200: RSS slope {slope:.3f} MiB/min ≥ "
            f"{SOAK_RSS_SLOPE_MAX_MIB_PER_MIN} MiB/min — a memory leak"
        )
    if growth >= SOAK_RSS_GROWTH_MAX_FRAC:
        raise AssertionError(
            f"SOAK-200: RSS grew {growth:.1%} ≥ {SOAK_RSS_GROWTH_MAX_FRAC:.0%} over "
            "the measurement window"
        )
    return slope, growth


@dataclass(frozen=True)
class LagSample:
    """One consumer-lag sample: ``t_s`` since start, ``lag_s`` (events-behind in s)."""

    t_s: float
    lag_s: float


def assert_lag_healthy(samples: Sequence[LagSample]) -> tuple[float, float]:
    """SOAK-200: lag slope ≤ 0 (within noise) AND p99 < 5 s.

    Returns ``(slope_s_per_s, p99_s)``. A small positive slope tolerance absorbs
    sampling noise; a sustained positive trend (lag growing) or a p99 above the
    5 s threshold is a failure."""
    if len(samples) < 2:
        raise AssertionError("SOAK-200: need ≥ 2 lag samples")
    slope = linear_regression_slope([s.t_s for s in samples], [s.lag_s for s in samples])
    p99 = percentile([s.lag_s for s in samples], 0.99)
    # A noise band: lag may wobble, but the regression must not show real growth.
    noise_band = 0.001  # s of lag per s of wall — effectively flat
    if slope > noise_band:
        raise AssertionError(
            f"SOAK-200: consumer lag is trending up (slope {slope:.4f} s/s > "
            f"{noise_band}) — the consumer is falling behind"
        )
    if p99 >= SOAK_LAG_P99_MAX_S:
        raise AssertionError(
            f"SOAK-200: lag p99 {p99:.2f} s ≥ {SOAK_LAG_P99_MAX_S} s threshold"
        )
    return slope, p99


def assert_tallies_reconcile(
    *, rest_total: int, ws_total: int, stats_total: int
) -> None:
    """SOAK-200: REST tally == WS tally == stats ``total_events`` at run end.

    At 200 TPS there are no WS drops, so all three independent counters must agree
    exactly — the "stats match an independent consumer-side tally" exit criterion."""
    if not (rest_total == ws_total == stats_total):
        raise AssertionError(
            "SOAK-200: end-of-run tallies disagree — "
            f"REST={rest_total} WS={ws_total} stats={stats_total} "
            "(all three must be equal at a no-drop rate)"
        )


def assert_staleness_ok(max_observed_staleness_s: float) -> None:
    """SOAK-200 / INV-OBS-2: stats staleness stayed ≤ 5 s throughout the run."""
    if max_observed_staleness_s > SOAK_STALENESS_MAX_S:
        raise AssertionError(
            f"SOAK-200: stats staleness reached {max_observed_staleness_s:.2f} s > "
            f"{SOAK_STALENESS_MAX_S} s (INV-OBS-2)"
        )


def assert_zero_error_logs(error_lines: Sequence[str]) -> None:
    """SOAK-200: zero ERROR-level log lines across all process groups."""
    if error_lines:
        sample = error_lines[0][:200]
        raise AssertionError(
            f"SOAK-200: {len(error_lines)} ERROR log line(s) across the soak "
            f"(first: {sample!r}) — the run must be error-free"
        )
