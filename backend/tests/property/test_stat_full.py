"""STAT-SHAPE-1/2 + STAT-F1..F12 + STAT-L1..L8 over the full manifest (§ STAT).

The Phase-8 statistical gates (phase-08 exit criteria #3/#4/#5):

* **PR-smoke** (``stat`` marker): STAT-F2/F4/F12 over a ~10k-session full-manifest
  batch (≈ 25 s) — the early conversion signal on the unit lane.
* **Nightly / Phase-8 gate** (``stat_nightly`` marker): the full STAT-SHAPE,
  STAT-F, and STAT-L catalogs over a large (50k-session / 30-sim-day) batch — the
  binding "realized conversion within tolerance at n ≥ 10k" + "30-day backfill shows
  diurnal/weekly shape" + "lifecycle latencies within tolerance" gates.

A window whose denominator is below the spec minimum returns the ``INSUFFICIENT``
sentinel and the runner turns it into a pytest **skip** (with the denominator in the
reason) — so a window the engine has not yet populated is skipped, never false-
failed, and activates the moment the data exists. Pure engine + ports; one batch
per profile feeds every check (no Postgres, no Redis).
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from tests.golden.harness_full import FullBatchResult, build_full_batch
from tests.property.stat_funnel import (
    ALL_FUNNEL_CHECKS,
    INSUFFICIENT,
    SMOKE_FUNNEL_CHECKS,
)
from tests.property.stat_latency import ALL_LATENCY_CHECKS, check_window
from tests.property.stat_shape import ALL_SHAPE_CHECKS
from tests.seeds import SEED_STAT

# A 10k-session smoke batch (~5 events/session ⇒ ~70k events).
SMOKE_EVENTS = 70_000
_US_PER_DAY = 86_400 * 1_000_000

# testing-strategy §5.2/§5.3 prescribe two distinct gate batches:
#
# * **Batch A — funnel & latency** (STAT-F*, STAT-L*): "n = 50,000 sessions" whose
#   lifecycles *fully resolve*. With the BE-F4-fixed backfill density (≈ 5,000
#   sessions/sim-day for the reference manifest), arrivals over ~10 sim-days give
#   ≥ 50k sessions; the window then drains to 60 sim-days so every spawned lifecycle
#   — including the 30-day return window (L5/F9) — completes. Capping *arrivals* (not
#   the window) is what keeps the realized parent→child ratios from being understated
#   by window-edge truncation (a delivery on the last arrival day still has its full
#   review/refund window inside the drained tail).
# * **Batch B — diurnal/weekly shape** (STAT-SHAPE-1/2): a full 30-sim-day backfill
#   (arrivals run the whole window) so the realized session-arrival histogram spans
#   complete diurnal x weekly cycles.
_FUNNEL_ARRIVAL_DAYS = 10
_FUNNEL_DRAIN_DAYS = 60
_SHAPE_DAYS = 30


@lru_cache(maxsize=1)
def _smoke_batch() -> FullBatchResult:
    return build_full_batch(seed=SEED_STAT, max_events=SMOKE_EVENTS)


@lru_cache(maxsize=1)
def _funnel_batch() -> FullBatchResult:
    """Batch A: ≥ 50k sessions (arrivals ≤ 10 sim-days) fully drained over 60 days."""
    return build_full_batch(
        seed=SEED_STAT,
        max_events=None,
        simulated_days=_FUNNEL_DRAIN_DAYS,
        arrival_until_us=_FUNNEL_ARRIVAL_DAYS * _US_PER_DAY,
    )


@lru_cache(maxsize=1)
def _shape_batch() -> FullBatchResult:
    """Batch B: a full 30-sim-day backfill (arrivals run the whole window)."""
    return build_full_batch(
        seed=SEED_STAT, max_events=None, simulated_days=_SHAPE_DAYS
    )


def _assert_or_skip(failure: str | None) -> None:
    if failure == INSUFFICIENT:
        pytest.skip("window denominator below the spec minimum (activates with volume)")
    assert failure is None, failure


@pytest.mark.stat
@pytest.mark.parametrize(
    "check_id,check", SMOKE_FUNNEL_CHECKS, ids=[c[0] for c in SMOKE_FUNNEL_CHECKS]
)
def test_stat_smoke_funnel(check_id: str, check: object) -> None:
    """PR-smoke funnel subset (STAT-F2/F4/F12) over the 10k-session batch."""
    _assert_or_skip(check(_smoke_batch()))  # type: ignore[operator]


@pytest.mark.stat_nightly
@pytest.mark.parametrize("check_id,check", ALL_SHAPE_CHECKS, ids=[c[0] for c in ALL_SHAPE_CHECKS])
def test_stat_shape(check_id: str, check: object) -> None:
    """STAT-SHAPE-1/2: 30-sim-day diurnal/weekly shape (exit #3)."""
    _assert_or_skip(check(_shape_batch()))  # type: ignore[operator]


@pytest.mark.stat_nightly
@pytest.mark.parametrize("check_id,check", ALL_FUNNEL_CHECKS, ids=[c[0] for c in ALL_FUNNEL_CHECKS])
def test_stat_funnel_full(check_id: str, check: object) -> None:
    """STAT-F1..F12: realized conversion within PRD tolerance at n ≥ 10k (exit #4)."""
    _assert_or_skip(check(_funnel_batch()))  # type: ignore[operator]


@pytest.mark.stat_nightly
@pytest.mark.parametrize("label,window", ALL_LATENCY_CHECKS, ids=[w[0] for w in ALL_LATENCY_CHECKS])
def test_stat_latency(label: str, window: object) -> None:
    """STAT-L1..L8: lifecycle latencies within ±15 % median / ±25 % p95 (exit #5)."""
    _assert_or_skip(check_window(_funnel_batch(), window))  # type: ignore[arg-type]
