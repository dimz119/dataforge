"""Stage-framework + PRF tests (chaos-engine §2.2, §4.1; CHD-1/2).

The structural stage-order test (the §2.2 normative order is fixed and the
pipeline iterates it), and the chaos sub-seed PRF determinism vectors.
"""

from __future__ import annotations

import hashlib
import hmac

from dataforge_engine.chaos import (
    STAGE_ORDER,
    ChaosPipeline,
    chaos_subseed,
    default_policy,
    draw_u,
    normative_stage_order,
)
from dataforge_engine.chaos.policy import CHAOS_MODES
from dataforge_engine.seeds import subseed

SEED = 424242


def test_normative_stage_order_is_frozen() -> None:
    """§2.2: the canonical order is exactly this sequence (structural)."""
    assert normative_stage_order() == (
        "missing",
        "duplicates",
        "corrupted_values",
        "nulls",
        "schema_drift",
        "out_of_order",
        "late_arriving",
    )
    assert STAGE_ORDER == CHAOS_MODES


def test_pipeline_runs_registered_modes_in_normative_order() -> None:
    """All registered modes run in the §2.2 normative order."""
    policy = default_policy()
    for mode in ("missing", "duplicates", "corrupted_values", "nulls"):
        policy[mode]["enabled"] = True
    pipeline = ChaosPipeline(policy)
    # The pipeline instantiates every REGISTERED stage in normative order
    # (enablement is read per-tick inside each stage, not at construction). The
    # registry order must be a prefix of (or equal to) STAGE_ORDER.
    from dataforge_engine.chaos.pipeline import STAGE_REGISTRY

    expected = [m for m in normative_stage_order() if m in STAGE_REGISTRY]
    assert pipeline.stage_modes == expected
    assert "schema_drift" in pipeline.stage_modes
    assert "out_of_order" in pipeline.stage_modes


def test_missing_precedes_duplicates_precedes_value_stages() -> None:
    """O-1/O-2/O-3: a dropped event can't be duplicated; copies precede mutation."""
    order = list(normative_stage_order())
    assert order.index("missing") < order.index("duplicates")
    assert order.index("duplicates") < order.index("corrupted_values")
    assert order.index("corrupted_values") < order.index("nulls")
    assert order.index("nulls") < order.index("late_arriving")


def test_chaos_subseed_matches_independent_hmac() -> None:
    """§4.1: chaos_subseed = HMAC-SHA256(BE64(seed), "chaos")."""
    expected = hmac.new(SEED.to_bytes(8, "big"), b"chaos", hashlib.sha256).digest()
    assert chaos_subseed(SEED) == expected
    assert chaos_subseed(SEED) == subseed(SEED, "chaos")
    assert len(chaos_subseed(SEED)) == 32


def test_prf_draw_matches_spec_formula() -> None:
    """§4.1: draw = first_8_bytes(HMAC(subseed, mode:event_id:label)) / 2**64."""
    sub = chaos_subseed(SEED)
    msg = b"missing:ev-1:select"
    digest = hmac.new(sub, msg, hashlib.sha256).digest()
    expected = int.from_bytes(digest[:8], "big") / 2**64
    assert draw_u(sub, "missing", "ev-1", "select") == expected


def test_prf_draw_independent_of_call_order_and_deterministic() -> None:
    """§4.2: a draw depends only on (mode, event_id, label) — never call order."""
    sub = chaos_subseed(SEED)
    a = draw_u(sub, "nulls", "ev-9", "field:0")
    _ = draw_u(sub, "corrupted_values", "ev-1", "select")  # interleave
    b = draw_u(sub, "nulls", "ev-9", "field:0")
    assert a == b
    # Distinct labels/modes/events give distinct draws (independence).
    assert draw_u(sub, "nulls", "ev-9", "select") != a
    assert draw_u(sub, "missing", "ev-9", "field:0") != a


def test_prf_instance_keying_distinguishes_copies() -> None:
    """§4.1 CR-2: instance-keyed draws differ per duplicate_index."""
    sub = chaos_subseed(SEED)
    assert draw_u(sub, "late_arriving", "ev-3", "select", 0) != draw_u(
        sub, "late_arriving", "ev-3", "select", 1
    )
