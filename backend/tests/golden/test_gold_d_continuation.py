"""GOLD-D — stop/restart continuation byte-identity (testing-strategy §6, §189).

The Phase-6 continuation gate (phase exit criterion #2, "resume with zero sequence
gaps"; pairs with OPS-4): generate N events, **stop**, restart a fresh
:class:`~dataforge_engine.behavior.Shard` from the §9.1 checkpoint, generate N more;
the concatenation is byte-identical to an uninterrupted 2N-event run (INV-STR-5,
T12 continuation). This proves the resume path — codec restore + pool-image reload
+ dwell/arrival/sequence rehydration — perturbs nothing: an interrupted run and an
uninterrupted run produce the same canonical stream.

Byte-identity is asserted over the wall-free :func:`content_only` projection
(occurred_at, sequence_no, payloads, refs, causality — everything that is a pure
function of the determinism unit), exactly as the §7.4 determinism-boundary test
does. ``emitted_at``/CDC ``ts_ms`` are wall-pacing artifacts: ``generate`` calls the
wall clock once per pass, and an interrupted run necessarily takes a different
number of passes, so the wall-domain fields legitimately differ — they are not part
of the determinism unit. On a mismatch the test reports the first divergent index,
its ``event_id``, and the field-level diff, so a continuation regression points at
the exact event.

Pure engine + ports (no Postgres, no Redis) — the fast golden lane. The runner's
live failover variant (SIGKILL takeover from the persisted checkpoint) is the
compose-only OPS-1/2 suite; this gates the codec-level continuation logic the
runner relies on, so a restore regression cannot slip past the PR lane.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dataforge_engine.envelope import canonical_serialize
from tests.golden.harness import build_batch, build_batch_with_restart, content_only
from tests.property.checks import ALL_CHECKS
from tests.seeds import SEED_GOLD_A

# A modest continuation unit: large enough that the stop point falls mid-funnel
# (sessions in flight, dwell timers pending, pools grown past the seed) so the
# restart genuinely exercises restore — small enough for the fast lane.
GOLD_D_STOP_AFTER = 500
GOLD_D_TOTAL = 1_000


def _field_diff(expected: str, actual: str) -> str:
    """A readable per-field diff of two content projections (mismatch report)."""
    try:
        exp = json.loads(expected)
        act = json.loads(actual)
    except json.JSONDecodeError:
        return f"  expected: {expected!r}\n  actual:   {actual!r}"
    keys = sorted(set(exp) | set(act))
    lines = [
        f"    {key}: expected={exp.get(key)!r} actual={act.get(key)!r}"
        for key in keys
        if exp.get(key) != act.get(key)
    ]
    return "\n".join(lines) or "  (content projections differ but parsed JSON is equal)"


@pytest.mark.golden
def test_gold_d_restart_continuation_is_byte_identical() -> None:
    """An interrupted run (stop + checkpoint + restart) == an uninterrupted run.

    The headline GOLD-D assertion: restoring from the §9.1 checkpoint and continuing
    yields the same canonical content as never stopping (INV-STR-5; the runner's
    resume + failover correctness, proven on the codec without a broker)."""
    uninterrupted = build_batch(seed=SEED_GOLD_A, max_events=GOLD_D_TOTAL)
    restarted = build_batch_with_restart(
        seed=SEED_GOLD_A, stop_after=GOLD_D_STOP_AFTER, total_events=GOLD_D_TOTAL
    )

    expected = [content_only(e) for e in uninterrupted.envelopes]
    actual = [content_only(e) for e in restarted.envelopes]

    assert actual, "GOLD-D produced no events"
    if len(actual) != len(expected):
        raise AssertionError(
            "GOLD-D event-count divergence: "
            f"uninterrupted={len(expected)} restarted={len(actual)} — the restart "
            "dropped or duplicated events (a continuation/sequence-gap regression)"
        )
    for index, (exp, act) in enumerate(zip(expected, actual, strict=True)):
        if exp != act:
            try:
                event_id = json.loads(act).get("event_id", "<unparseable>")
            except json.JSONDecodeError:
                event_id = "<unparseable>"
            raise AssertionError(
                f"GOLD-D continuation divergence at index {index} "
                f"(event_id={event_id}):\n{_field_diff(exp, act)}\n"
                "A restart from checkpoint must reproduce the uninterrupted run "
                "(INV-STR-5, T12 continuation)."
            )


@pytest.mark.golden
def test_gold_d_restart_sequence_is_gapless_across_the_stop() -> None:
    """The restarted stream's ``sequence_no`` is gapless 1..N across the stop point.

    The continuation's own structural guarantee (INV-GEN-7): the checkpoint carried
    the sequence counter, so segment two resumes it with zero gaps — the engine-level
    proof behind the demo's ``jq ... max == 1`` contiguity check (phase-06 demo #5)."""
    restarted = build_batch_with_restart(
        seed=SEED_GOLD_A, stop_after=GOLD_D_STOP_AFTER, total_events=GOLD_D_TOTAL
    )
    seqs = [e["sequence_no"] for e in restarted.envelopes]
    assert seqs == list(range(1, len(seqs) + 1)), (
        "GOLD-D restart introduced a sequence_no gap or duplicate across the stop "
        "(INV-GEN-7): the checkpoint must carry the gapless counter"
    )


@pytest.mark.golden
@pytest.mark.parametrize("check_id,check", ALL_CHECKS, ids=[c[0] for c in ALL_CHECKS])
def test_gold_d_restart_passes_referential_integrity(check_id: str, check: Any) -> None:
    """PROP-RI-1..8 hold over the restarted run — resume causes zero integrity
    violations (phase-06 exit #2; OPS-4 "zero integrity violations" on the codec)."""
    restarted = build_batch_with_restart(
        seed=SEED_GOLD_A, stop_after=GOLD_D_STOP_AFTER, total_events=GOLD_D_TOTAL
    )
    failure = check(restarted)
    assert failure is None, failure


@pytest.mark.golden
def test_gold_d_stop_point_is_mid_run_not_a_clean_boundary() -> None:
    """Guard the test's own potency: the stop point must land with state in flight.

    If the first segment drained the whole funnel (empty heap, no in-session actors)
    the restart would restore a trivial empty state and the byte-identity above would
    be vacuous. Assert the uninterrupted run actually has events beyond the stop point
    AND that the full envelope (incl. ``emitted_at``) differs from content — proving
    we are projecting away a real wall field, not comparing a no-op."""
    uninterrupted = build_batch(seed=SEED_GOLD_A, max_events=GOLD_D_TOTAL)
    assert len(uninterrupted.envelopes) > GOLD_D_STOP_AFTER, (
        "the stop point is at/after the end of the run — pick a larger total so the "
        "restart resumes genuine in-flight state"
    )
    full = [canonical_serialize(e) for e in uninterrupted.envelopes]
    proj = [content_only(e).encode() for e in uninterrupted.envelopes]
    assert full != proj, "content_only removed nothing — the projection is a no-op"
