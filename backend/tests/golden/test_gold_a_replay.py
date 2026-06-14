"""GOLD-A — byte-identical replay against the committed golden fixture (§6).

The permanent Phase-4 golden gate (testing-strategy §6, phase exit criterion #1):
the generic engine, run at the pinned seed over the builtin subset manifest under
a **deterministic injected wall clock**, must reproduce the committed
``events.jsonl.gz`` **byte-for-byte** — the *full* envelope, wall ``emitted_at``
included. This proves INV-GEN-3 / INV-G-4 / PIN-1: canonical content is a pure
function of ``(manifest_version, seed, merged config)`` and the simulated clock,
never wall pacing, pass sizes, or batch boundaries (behavior-engine §7.4).

On a mismatch the harness reports the **first divergent line number, its
``event_id``, and a field-level diff** of that event — so a determinism
regression points at the exact event and field, not "the file changed".

CI never regenerates the fixture; ``make golden-regen`` is the only (local,
``golden-rebaseline``-labelled) way to move the baseline. Pure engine + ports — no
Postgres, no Redis — so this runs in the fast engine/golden lane.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from dataforge_engine.envelope import canonical_serialize
from tests.golden.harness import build_batch, content_only
from tests.seeds import SEED_GOLD_A

_FIXTURE_DIR = Path(__file__).resolve().parent / "ecommerce" / "1.0.0" / "gold-a-1k"
_EVENTS_FILE = _FIXTURE_DIR / "events.jsonl.gz"
_META_FILE = _FIXTURE_DIR / "meta.json"


def _golden_lines() -> list[bytes]:
    """The committed golden batch, one canonical envelope per line (no trailing nl)."""
    with gzip.open(_EVENTS_FILE, "rb") as fh:
        blob = fh.read()
    return [line for line in blob.split(b"\n") if line]


def _meta() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_META_FILE.read_text(encoding="utf-8"))
    return data


def _field_diff(expected: bytes, actual: bytes) -> str:
    """A readable per-field diff of two serialized envelopes (mismatch report)."""
    try:
        exp = json.loads(expected)
        act = json.loads(actual)
    except json.JSONDecodeError:
        return f"  expected: {expected!r}\n  actual:   {actual!r}"
    keys = sorted(set(exp) | set(act))
    lines = []
    for key in keys:
        if exp.get(key) != act.get(key):
            lines.append(f"    {key}: expected={exp.get(key)!r} actual={act.get(key)!r}")
    return "\n".join(lines) or "  (serialized bytes differ but parsed JSON is equal — key order?)"


@pytest.mark.golden
def test_gold_a_meta_is_well_formed() -> None:
    """The fixture metadata records the PIN-1 determinism unit (seed pinned to §16.1)."""
    meta = _meta()
    assert meta["seed"] == SEED_GOLD_A
    assert meta["scenario_slug"] == "ecommerce"
    assert meta["manifest_version"] == "1.0.0"
    assert meta["envelope_version"] == "1.0"
    assert meta["event_count"] == len(_golden_lines())


@pytest.mark.golden
def test_gold_a_replays_byte_identically() -> None:
    """Replaying the pinned seed reproduces the committed fixture byte-for-byte."""
    expected = _golden_lines()
    result = build_batch(seed=SEED_GOLD_A, max_events=len(expected))
    actual = [canonical_serialize(env) for env in result.envelopes]

    assert actual, "the golden replay produced no events"
    if len(actual) != len(expected):
        raise AssertionError(
            "GOLD-A event-count divergence: "
            f"committed={len(expected)} replayed={len(actual)} "
            "(a count change is a determinism regression unless golden-rebaselined)"
        )
    for line_no, (exp, act) in enumerate(zip(expected, actual, strict=True)):
        if exp != act:
            try:
                event_id = json.loads(act).get("event_id", "<unparseable>")
            except json.JSONDecodeError:
                event_id = "<unparseable>"
            raise AssertionError(
                f"GOLD-A byte divergence at line {line_no} (event_id={event_id}):\n"
                f"{_field_diff(exp, act)}\n"
                "Run `make golden-regen` only with an intentional, "
                "golden-rebaseline-labelled change (testing-strategy §6)."
            )


@pytest.mark.golden
def test_gold_a_same_seed_is_stable_across_two_live_runs() -> None:
    """Two fresh in-process runs at the pinned seed are byte-identical (no fixture)."""
    a = [canonical_serialize(e) for e in build_batch(seed=SEED_GOLD_A, max_events=500).envelopes]
    b = [canonical_serialize(e) for e in build_batch(seed=SEED_GOLD_A, max_events=500).envelopes]
    assert a and a == b


@pytest.mark.golden
def test_gold_a_different_seed_diverges() -> None:
    """A different seed yields a different canonical batch (no accidental fixity)."""
    a = [canonical_serialize(e) for e in build_batch(seed=SEED_GOLD_A, max_events=300).envelopes]
    diff = build_batch(seed=SEED_GOLD_A + 1, max_events=300)
    c = [canonical_serialize(e) for e in diff.envelopes]
    assert a != c


@pytest.mark.golden
def test_gold_a_content_is_invariant_to_pass_size() -> None:
    """The determinism boundary (behavior-engine §7.4): canonical *content* is a pure
    function of (manifest, seed, config) and the simulated clock — it must NOT vary
    with pass sizes / tick boundaries. Only wall-domain fields (``emitted_at``, CDC
    ``ts_ms``) may differ, because the deterministic wall clock is called a different
    number of times per pass. Run the same seed at three pass sizes and assert the
    content projection is byte-identical (and that the wall fields do change, so the
    test is exercising the boundary, not a no-op)."""
    big = build_batch(seed=SEED_GOLD_A, max_events=600, pass_size=600)
    small = build_batch(seed=SEED_GOLD_A, max_events=600, pass_size=29)
    medium = build_batch(seed=SEED_GOLD_A, max_events=600, pass_size=137)

    content_big = [content_only(e) for e in big.envelopes]
    content_small = [content_only(e) for e in small.envelopes]
    content_medium = [content_only(e) for e in medium.envelopes]
    assert content_big and content_big == content_small == content_medium, (
        "canonical content diverged across pass sizes — a §7.4 determinism-boundary "
        "violation (content must not depend on pass/tick boundaries)"
    )
    # The wall-domain fields are allowed to (and do) differ across pass schedules —
    # confirm the projection actually removed a varying field (guards a no-op test).
    full_big = [canonical_serialize(e) for e in big.envelopes]
    full_small = [canonical_serialize(e) for e in small.envelopes]
    assert full_big != full_small, "expected emitted_at to vary across pass sizes"
