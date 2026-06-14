"""PROP-RI-1..8 over the 1,000,000-event profile (Phase-4 exit criterion #2).

The nightly / attended-gate referential-integrity run: the *same* eight PROP-RI
checks as the PR profile, over a **1,000,000-event** canonical batch at the pinned
seed (testing-strategy §4.1; phase-04 exit criterion #2 — "referential validity
over a 1M-event batch"). Too slow for the PR lane, so it carries the dedicated
``property_nightly`` marker and runs nightly + at the attended Phase-4 gate, never
in per-PR CI (the PR lane runs only the 100k profile).

Pure engine + ports — no Postgres, no Redis. One generated batch feeds all checks.
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from tests.golden.harness import BatchResult, build_batch
from tests.property.checks import ALL_CHECKS
from tests.seeds import SEED_GOLD_A

NIGHTLY_PROFILE_EVENTS = 1_000_000


@lru_cache(maxsize=1)
def _nightly_batch() -> BatchResult:
    """The shared 1M nightly/gate batch (generated once for all property checks)."""
    return build_batch(seed=SEED_GOLD_A, max_events=NIGHTLY_PROFILE_EVENTS)


@pytest.mark.property_nightly
def test_nightly_profile_reaches_target_volume() -> None:
    """The batch reaches the 1M bound (the gate is over a million events)."""
    batch = _nightly_batch()
    assert len(batch.envelopes) >= NIGHTLY_PROFILE_EVENTS - 10, (
        f"nightly profile produced only {len(batch.envelopes)} events; the 30-day "
        "window/rates must sustain ~1M for the Phase-4 gate to be meaningful"
    )


@pytest.mark.property_nightly
@pytest.mark.parametrize("check_id,check", ALL_CHECKS, ids=[c[0] for c in ALL_CHECKS])
def test_nightly_profile_referential_integrity(check_id: str, check: object) -> None:
    """Each PROP-RI invariant holds over the 1M-event batch (exit criterion #2)."""
    batch = _nightly_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure
