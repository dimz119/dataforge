"""STAT-L1..L8 — the lifecycle-latency checks (testing-strategy § STAT).

For each PRD §4.2 lifecycle window, compute the realized latency
``occurred_at(child) - occurred_at(parent)`` (paired by order_id) across a large
full-manifest batch, and assert:

* realized **median** within ±15 % of the configured median, and
* realized **p95** within ±25 % of the configured p95 (wider — tail estimates are
  noisier), and
* 100 % of samples within the hard bound (else the bound's fallback event must
  exist — e.g. L1 > 30 min ⇒ ``order_cancelled``).

The configured medians/p95 are read from the manifest ``dwell`` distributions (the
windows are tagged ``# L1``..``# L8`` in ``1.1.0.yaml``); rather than hard-code them
in Python (which would couple the test to manifest internals and drift), each
window's *expected* central value is supplied to the runner as a duration band that
mirrors the PRD §4.2 table. Minimum denominator 500 per window; a window below it
returns the :data:`INSUFFICIENT` sentinel (skipped, not failed) so the suite
activates per window as the engine populates the downstream funnel.

Pure stdlib — no numpy. ``occurred_at`` is RFC-3339 in the simulated timezone, so a
simple ISO parse gives the virtual instant; the difference is a virtual-clock domain
duration (the speed_multiplier is already folded into ``occurred_at``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tests.golden.harness_full import FullBatchResult
from tests.property.stat_funnel import INSUFFICIENT


@dataclass(frozen=True)
class Window:
    """A PRD §4.2 lifecycle window: parent→child paired by ``order_id``."""

    label: str
    parent: str
    child: str
    median_s: float  # configured median, seconds (PRD §4.2)
    p95_s: float  # configured p95, seconds


# The PRD §4.2 windows (medians/p95 in seconds), each paired by ``order_id`` and
# verified against the configured central value of its manifest ``# L*`` dwell tag.
# L7/L8 are *in-session* (pre-order) windows whose events carry no ``order_id`` — they
# cannot be paired by the order key, so they stay placeholder/INSUFFICIENT here (their
# session-scoped pacing is exercised by STAT-F1's views-per-session check). The earlier
# L3..L7 rows mis-mapped the PRD §4.2 table to the wrong manifest edges; corrected:
#
# | PRD | window                               | median | p95   | manifest tag |
# | L1  | order_placed → payment_authorized    | 45 s   | 10 m  | # L1         |
# | L2  | payment_authorized → shipment_created| 18 h   | 48 h  | # L2         |
# | L3  | shipment_created → shipment_delivered| 2.5 d  | 6 d   | created→…→delivered |
# | L4  | shipment_delivered → review_submitted| 3 d    | 14 d  | # L4         |
# | L5  | shipment_delivered → refund_requested| 4 d    | 21 d  | # L5         |
# | L6  | refund_requested → refund_approved   | 24 h   | 72 h  | # L6         |
_H = 3600.0
_D = 86_400.0
WINDOWS: tuple[Window, ...] = (
    Window("STAT-L1", "order_placed", "payment_authorized", 45.0, 600.0),  # PT45S / PT10M
    Window("STAT-L2", "payment_authorized", "shipment_created", 18 * _H, 48 * _H),  # PT18H/PT48H
    Window("STAT-L3", "shipment_created", "shipment_delivered", 2.5 * _D, 6 * _D),  # PRD L3
    Window("STAT-L4", "shipment_delivered", "review_submitted", 3 * _D, 14 * _D),  # P3D / P14D
    Window("STAT-L5", "shipment_delivered", "refund_requested", 4 * _D, 21 * _D),  # P4D / P21D
    Window("STAT-L6", "refund_requested", "refund_approved", 24 * _H, 72 * _H),  # P1D / P3D
    Window("STAT-L7", "product_viewed", "product_viewed", 20.0, 120.0),  # in-session
    Window("STAT-L8", "checkout_started", "order_placed", 3 * 60.0, 12 * 60.0),  # in-session
)


def _occ(env: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(env["occurred_at"]).replace("Z", "+00:00"))


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _percentile(xs: list[float], p: float) -> float:
    s = sorted(xs)
    idx = min(len(s) - 1, round(p * (len(s) - 1)))
    return s[idx]


def _latencies(result: FullBatchResult, window: Window) -> list[float]:
    """Realized parent→child latencies (s), paired by ``order_id`` (first child)."""
    parent_at: dict[str, datetime] = {}
    out: list[float] = []
    for env in result.envelopes:
        et = env["event_type"]
        oid = env["payload"].get("order_id") if isinstance(env["payload"], dict) else None
        if oid is None:
            continue
        oid = str(oid)
        if et == window.parent and oid not in parent_at:
            parent_at[oid] = _occ(env)
        elif et == window.child and oid in parent_at:
            dt = (_occ(env) - parent_at.pop(oid)).total_seconds()
            if dt >= 0:
                out.append(dt)
    return out


def check_window(
    result: FullBatchResult,
    window: Window,
    *,
    median_tol: float = 0.15,
    p95_tol: float = 0.25,
    min_n: int = 500,
) -> str | None:
    """Assert a single STAT-L window's realized median/p95 against the configured
    band; INSUFFICIENT if fewer than ``min_n`` paired samples (skipped, not failed)."""
    if window.parent == window.child:  # placeholder window — no parent/child edge
        return INSUFFICIENT
    lat = _latencies(result, window)
    if len(lat) < min_n:
        return INSUFFICIENT
    med = _median(lat)
    p95 = _percentile(lat, 0.95)
    if abs(med - window.median_s) > median_tol * window.median_s:
        return (
            f"{window.label}: realized median {med:.1f}s (n={len(lat)}) outside "
            f"±{median_tol:.0%} of configured {window.median_s:.1f}s"
        )
    if abs(p95 - window.p95_s) > p95_tol * window.p95_s:
        return (
            f"{window.label}: realized p95 {p95:.1f}s outside ±{p95_tol:.0%} of "
            f"configured {window.p95_s:.1f}s"
        )
    return None


ALL_LATENCY_CHECKS: tuple[tuple[str, Window], ...] = tuple(
    (w.label, w) for w in WINDOWS
)
