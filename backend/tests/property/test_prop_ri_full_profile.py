"""PROP-RI-1..8 over the FULL manifest (1.1.0 + CDC) — PR profile + 1M nightly gate.

The Phase-8 referential-integrity gate over the full 8-entity manifest with CDC
(phase-08 exit criteria #1 and #8). The same eight invariants as the subset profile
but with the full-manifest refund-gate (RI-3), inventory reconciliation (RI-4), and
the 8-cdc-subject schema frame (RI-8) — see :mod:`tests.property.prop_ri_full`.

* **PR profile** (``property`` marker): a 60k-event full-manifest batch at
  ``SEED_SOAK`` — fast enough for the unit lane, large enough that the funnel +
  CDC + background mutations are all exercised, so RI-1..8 are non-vacuous.
* **1M nightly** (``property_nightly`` marker): the same checks over a 1,000,000-
  event batch — the binding "1M-event soak, zero integrity violations" gate
  (phase-08 exit #1). Too slow for the PR lane; runs nightly + at the attended gate.

Pure engine + ports — one generated batch per profile feeds all checks.
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from tests.golden.harness_full import FullBatchResult, build_full_batch
from tests.property.prop_ri_full import ALL_FULL_CHECKS
from tests.seeds import SEED_SOAK

FULL_PR_EVENTS = 60_000
FULL_NIGHTLY_EVENTS = 1_000_000


@lru_cache(maxsize=1)
def _pr_batch() -> FullBatchResult:
    return build_full_batch(seed=SEED_SOAK, max_events=FULL_PR_EVENTS)


@lru_cache(maxsize=1)
def _nightly_batch() -> FullBatchResult:
    return build_full_batch(seed=SEED_SOAK, max_events=FULL_NIGHTLY_EVENTS)


@pytest.mark.property
def test_full_pr_profile_reaches_volume_and_emits_cdc() -> None:
    """The PR batch reaches volume and carries CDC (the properties are non-vacuous)."""
    batch = _pr_batch()
    assert len(batch.envelopes) >= FULL_PR_EVENTS - 10, (
        f"full PR profile produced only {len(batch.envelopes)} events"
    )
    assert any(str(e["event_type"]).startswith("cdc.") for e in batch.envelopes), (
        "the full-manifest PR profile produced no CDC rows"
    )


@pytest.mark.property
@pytest.mark.parametrize("check_id,check", ALL_FULL_CHECKS, ids=[c[0] for c in ALL_FULL_CHECKS])
def test_full_pr_profile_referential_integrity(check_id: str, check: object) -> None:
    """Each PROP-RI invariant holds over the 60k full-manifest PR batch."""
    batch = _pr_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure


@pytest.mark.property_nightly
def test_full_nightly_profile_reaches_target_volume() -> None:
    """The nightly batch reaches the 1M bound (the soak gate is over a million events)."""
    batch = _nightly_batch()
    assert len(batch.envelopes) >= FULL_NIGHTLY_EVENTS - 10, (
        f"full nightly profile produced only {len(batch.envelopes)} events"
    )


@pytest.mark.property_nightly
@pytest.mark.parametrize("check_id,check", ALL_FULL_CHECKS, ids=[c[0] for c in ALL_FULL_CHECKS])
def test_full_nightly_profile_referential_integrity(check_id: str, check: object) -> None:
    """Each PROP-RI invariant holds over the 1M full-manifest batch (exit #1)."""
    batch = _nightly_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure
