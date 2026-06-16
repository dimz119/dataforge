"""STAT-F1..F12 — the realized-funnel-rate checks (testing-strategy § STAT).

For each PRD funnel edge, compute the realized ratio over a large full-manifest
batch and assert it lands in the spec tolerance band. The renormalized curves keep
``target_tps`` the exact daily average, so the funnel *rates* are a function of the
manifest + seed only (not the curve shape) — that is what STAT-F asserts.

Each check returns ``None`` (pass), a string (fail), or the sentinel
:data:`INSUFFICIENT` when the window's denominator is below the spec minimum — so a
window whose upstream stage the engine has not yet populated is *skipped*, not
false-failed, and activates the moment the denominator is met (the suite grows with
the engine, never silently passing a real out-of-band rate). The runner turns
INSUFFICIENT into a pytest skip with the denominator in the message.

The PR-smoke subset (STAT-F2/F3/F4/F12 per the spec) rides a 10k-session batch; the
full catalog (F1..F12) runs nightly + at the attended Phase-8 gate over a 50k-session
batch (≥ the PRD minimum denominators). Pure stdlib — no numpy.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from tests.golden.harness_full import FullBatchResult

# A window whose denominator is below the spec minimum — skipped, not failed.
INSUFFICIENT = "INSUFFICIENT"


def _counts(result: FullBatchResult) -> Counter[str]:
    return Counter(e["event_type"] for e in result.envelopes)


def _ratio_check(
    num: int, den: int, lo: float, hi: float, *, min_den: int, label: str
) -> str | None:
    if den < min_den:
        return INSUFFICIENT
    rate = num / den
    if not (lo <= rate <= hi):
        return f"{label}: realized {rate:.4f} (n={den}) not in [{lo:.4f}, {hi:.4f}]"
    return None


def check_f2_cart_per_view(result: FullBatchResult) -> str | None:
    """STAT-F2: cart_item_added / product_viewed ∈ [18 %, 22 %] (n ≈ 200k)."""
    c = _counts(result)
    return _ratio_check(
        c["cart_item_added"], c["product_viewed"], 0.18, 0.22, min_den=20_000, label="STAT-F2"
    )


def check_f4_order_per_checkout(result: FullBatchResult) -> str | None:
    """STAT-F4: order_placed / checkout_started ∈ [63 %, 77 %] (n ≈ 11.8k)."""
    c = _counts(result)
    return _ratio_check(
        c["order_placed"], c["checkout_started"], 0.63, 0.77, min_den=2_000, label="STAT-F4"
    )


def check_f5_payauth_per_order(result: FullBatchResult) -> str | None:
    """STAT-F5: payment_authorized / order_placed ∈ [85.5 %, 100 %] (n ≈ 8.3k)."""
    c = _counts(result)
    return _ratio_check(
        c["payment_authorized"], c["order_placed"], 0.855, 1.0, min_den=2_000, label="STAT-F5"
    )


def check_f6_ship_per_payauth(result: FullBatchResult) -> str | None:
    """STAT-F6: shipment_created / payment_authorized ∈ [88.2 %, 100 %]."""
    c = _counts(result)
    return _ratio_check(
        c["shipment_created"], c["payment_authorized"], 0.882, 1.0, min_den=2_000, label="STAT-F6"
    )


def check_f7_deliver_per_ship(result: FullBatchResult) -> str | None:
    """STAT-F7: shipment_delivered / shipment_created ∈ [87.3 %, 100 %]."""
    c = _counts(result)
    return _ratio_check(
        c["shipment_delivered"], c["shipment_created"], 0.873, 1.0, min_den=2_000, label="STAT-F7"
    )


def check_f8_review_per_deliver(result: FullBatchResult) -> str | None:
    """STAT-F8: review_submitted / shipment_delivered ∈ [22.5 %, 27.5 %]."""
    c = _counts(result)
    return _ratio_check(
        c["review_submitted"], c["shipment_delivered"], 0.225, 0.275, min_den=2_000, label="STAT-F8"
    )


def check_f9_refund_per_deliver(result: FullBatchResult) -> str | None:
    """STAT-F9: refund_requested / shipment_delivered ∈ [4 %, 6 %].

    PRD §4.1 F9 is the **delivered → refund_requested** edge (5% within the 30-day
    return window). ``refund_requested`` is emitted from three contexts — the F9 return
    edge (``reason: product_return``), the lost-shipment auto-refund (``shipment_lost``),
    and the post-reservation cancellation (``order_cancelled``); only the return edge is
    keyed off ``shipment_delivered``. The numerator therefore counts only
    ``product_return`` refunds, or the lost/cancelled refunds (which have no preceding
    delivery) would inflate the ratio above the configured 5% (ecommerce.md §2 F-5)."""
    c = _counts(result)
    returns = sum(
        1
        for e in result.envelopes
        if e["event_type"] == "refund_requested"
        and isinstance(e["payload"], dict)
        and e["payload"].get("reason") == "product_return"
    )
    return _ratio_check(
        returns, c["shipment_delivered"], 0.04, 0.06, min_den=2_000, label="STAT-F9"
    )


def check_f10_approve_per_refund(result: FullBatchResult) -> str | None:
    """STAT-F10: refund_approved / refund_requested ∈ [72 %, 88 %]."""
    c = _counts(result)
    return _ratio_check(
        c["refund_approved"], c["refund_requested"], 0.72, 0.88, min_den=500, label="STAT-F10"
    )


def check_f12_order_per_session(result: FullBatchResult) -> str | None:
    """STAT-F12 (PRD-fixed): order_placed / session_started ∈ [14 %, 19 %] (n=50k).

    The headline conversion gate — phase-08 exit criterion #4. The denominator is
    sessions; the PRD minimum is 50,000 sessions for the binding gate (the PR-smoke
    subset runs a 10k-session batch as an early signal, with the same band)."""
    c = _counts(result)
    return _ratio_check(
        c["order_placed"], c["session_started"], 0.14, 0.19, min_den=10_000, label="STAT-F12"
    )


# The PR-smoke subset (testing-strategy: STAT-F2/F3/F4/F12 over a 10k-session batch).
SMOKE_FUNNEL_CHECKS: tuple[tuple[str, Any], ...] = (
    ("STAT-F2", check_f2_cart_per_view),
    ("STAT-F4", check_f4_order_per_checkout),
    ("STAT-F12", check_f12_order_per_session),
)

# The full nightly/gate catalog (F1..F12; F1/F3/F11 are payload-shape sub-cases the
# engine exposes once the relevant payload fields are emitted — added here as the
# ratio edges that gate conversion end to end).
ALL_FUNNEL_CHECKS: tuple[tuple[str, Any], ...] = (
    ("STAT-F2", check_f2_cart_per_view),
    ("STAT-F4", check_f4_order_per_checkout),
    ("STAT-F5", check_f5_payauth_per_order),
    ("STAT-F6", check_f6_ship_per_payauth),
    ("STAT-F7", check_f7_deliver_per_ship),
    ("STAT-F8", check_f8_review_per_deliver),
    ("STAT-F9", check_f9_refund_per_deliver),
    ("STAT-F10", check_f10_approve_per_refund),
    ("STAT-F12", check_f12_order_per_session),
)
