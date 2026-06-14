"""PROP-RI-1..8 over the 100k-event PR profile (testing-strategy §4.1, §14 Phase 4).

The per-PR referential-integrity gate: generate a 100,000-event canonical batch at
the pinned ``SEED_GOLD_A`` seed over the builtin subset manifest (the engine is the
seeded strategy, §17.1) and assert every PROP-RI invariant. One batch, all eight
checks — entity-ref resolution, payment⇒order, gapless ``sequence_no``, monotone
``occurred_at``, causality-chain resolution, ``schema_ref`` resolution, the 20-key
envelope (INV-GEN-1/2/4/7, INV-REG-4).

The 1,000,000-event profile is the same checks at a larger bound but too slow for
the PR lane — it lives in ``test_prop_ri_nightly.py`` under the ``property_nightly``
marker (run nightly + at the attended Phase-4 gate). Pure engine + ports: no
Postgres, no Redis — the fast engine lane.
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from tests.golden.harness import BatchResult, build_batch
from tests.property.checks import ALL_CHECKS
from tests.seeds import SEED_GOLD_A

PR_PROFILE_EVENTS = 100_000


@lru_cache(maxsize=1)
def _pr_batch() -> BatchResult:
    """The shared 100k PR-profile batch (generated once for all property checks)."""
    return build_batch(seed=SEED_GOLD_A, max_events=PR_PROFILE_EVENTS)


@pytest.mark.property
def test_pr_profile_reaches_target_volume() -> None:
    """The batch reaches the 100k bound (the properties are vacuous on a tiny batch)."""
    batch = _pr_batch()
    assert len(batch.envelopes) >= PR_PROFILE_EVENTS - 10, (
        f"PR profile produced only {len(batch.envelopes)} events; the window/rates "
        "must sustain ~100k for the property gate to be meaningful"
    )


@pytest.mark.property
@pytest.mark.parametrize("check_id,check", ALL_CHECKS, ids=[c[0] for c in ALL_CHECKS])
def test_pr_profile_referential_integrity(check_id: str, check: object) -> None:
    """Each PROP-RI invariant holds over the 100k PR-profile batch."""
    batch = _pr_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure
