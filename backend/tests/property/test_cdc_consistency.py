"""CDC-1..7 over the full-manifest batch (testing-strategy § CDC; phase-08 exit #2).

The CDC-consistency gate: generate a full-manifest (1.1.0) canonical batch at the
pinned ``SEED_GOLD_B`` seed and assert every CDC invariant — no ``u``/``d`` before
``c``/``r``, gapless before-image chaining, business/CDC adjacency, business-stream
reconciliation, background chain roots, snapshot ``r`` shape, op/image null-rules.
One batch, all seven checks (the engine is the seeded strategy).

CDC events derive from the SAME pool mutation as their business event (ADR-0012),
so a CDC regression is a divergence between the two views — these checks catch it
before it reaches a consumer (INV-GEN-6).

* The **PR subset** (``cdc`` marker) runs over a ~10k batch (fast — rides the unit
  lane as the permanent per-PR CDC gate, R-CDC-4 named in the spec as permanent CI).
* The **nightly full** (``stat_nightly`` marker) runs the same checks over a much
  larger batch so background-mutation and reconciliation paths have volume.

Pure engine + ports — no Postgres, no Redis (the fast engine lane).
"""

from __future__ import annotations

from functools import lru_cache

import pytest

from tests.golden.harness_full import FullBatchResult, build_full_batch
from tests.property.cdc_checks import ALL_CDC_CHECKS
from tests.seeds import SEED_GOLD_B

CDC_PR_EVENTS = 10_000
CDC_NIGHTLY_EVENTS = 200_000


@lru_cache(maxsize=1)
def _pr_batch() -> FullBatchResult:
    return build_full_batch(seed=SEED_GOLD_B, max_events=CDC_PR_EVENTS)


@lru_cache(maxsize=1)
def _nightly_batch() -> FullBatchResult:
    return build_full_batch(seed=SEED_GOLD_B, max_events=CDC_NIGHTLY_EVENTS)


@pytest.mark.cdc
def test_cdc_pr_batch_actually_emits_cdc() -> None:
    """The PR batch carries CDC rows — the checks are vacuous on a CDC-free batch."""
    batch = _pr_batch()
    cdc = [e for e in batch.envelopes if str(e["event_type"]).startswith("cdc.")]
    assert cdc, "the full-manifest PR batch produced no CDC rows — CDC is not firing"
    ops = {e["op"] for e in cdc}
    assert ops & {"c", "u"}, f"expected c/u CDC ops, saw {ops!r}"


@pytest.mark.cdc
@pytest.mark.parametrize("check_id,check", ALL_CDC_CHECKS, ids=[c[0] for c in ALL_CDC_CHECKS])
def test_cdc_pr_consistency(check_id: str, check: object) -> None:
    """Each CDC-1..7 invariant holds over the PR full-manifest batch (exit #2)."""
    batch = _pr_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure


@pytest.mark.stat_nightly
@pytest.mark.parametrize("check_id,check", ALL_CDC_CHECKS, ids=[c[0] for c in ALL_CDC_CHECKS])
def test_cdc_nightly_consistency(check_id: str, check: object) -> None:
    """Each CDC-1..7 invariant holds over the larger nightly full-manifest batch."""
    batch = _nightly_batch()
    failure = check(batch)  # type: ignore[operator]
    assert failure is None, failure
