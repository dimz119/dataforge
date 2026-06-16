"""GOLD-B — byte-identical replay of the FULL manifest + CDC fixture (§6; exit #6).

Phase-8 exit criterion #6 (permanent): the generic engine, run at ``SEED_GOLD_B``
over the **full** builtin manifest (ecommerce 1.1.0 — 8 entities, ~21 business
event types, 4 default-on CDC subjects, diurnal/weekly intensity curves) under a
**deterministic injected wall clock**, must reproduce the committed
``events.jsonl.gz`` **byte-for-byte** — the full envelope, wall ``emitted_at``
included, with the CDC ``c``/``u`` rows interleaved in ``sequence_no`` order.

This proves determinism survives CDC + curves: canonical content is a pure function
of ``(manifest_version, seed, merged config)`` and the simulated clock — the curve
shape changes *when* arrivals land (so the diurnal/weekly shape is visible, STAT-
SHAPE) but never the byte sequence at a fixed seed, and the CDC views derive from
the same pool mutations (ADR-0012) so they can never drift from the business rows.

The mismatch report (first divergent line / event_id / field diff) and the
re-baselining policy are identical to GOLD-A. CI never regenerates the fixture;
``make golden-regen-b`` is the only (local, ``golden-rebaseline``-labelled) way.
Pure engine + ports — the fast golden lane (no Postgres, no Redis).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from dataforge_engine.envelope import canonical_serialize
from tests.golden.harness_full import build_full_batch, full_ecommerce_document
from tests.golden.test_gold_a_replay import _field_diff
from tests.seeds import SEED_GOLD_B

_FIXTURE_DIR = Path(__file__).resolve().parent / "ecommerce" / "1.1.0" / "gold-b-10k"
_EVENTS_FILE = _FIXTURE_DIR / "events.jsonl.gz"
_META_FILE = _FIXTURE_DIR / "meta.json"


def _golden_lines() -> list[bytes]:
    with gzip.open(_EVENTS_FILE, "rb") as fh:
        blob = fh.read()
    return [line for line in blob.split(b"\n") if line]


def _meta() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_META_FILE.read_text(encoding="utf-8"))
    return data


@pytest.mark.golden
def test_gold_b_meta_is_well_formed() -> None:
    """The fixture metadata records the PIN-1 determinism unit for the full manifest."""
    meta = _meta()
    assert meta["seed"] == SEED_GOLD_B
    assert meta["scenario_slug"] == "ecommerce"
    assert meta["manifest_version"] == "1.1.0"
    assert meta["envelope_version"] == "1.0"
    assert meta["event_count"] == len(_golden_lines())


@pytest.mark.golden
def test_gold_b_replays_byte_identically() -> None:
    """Replaying ``SEED_GOLD_B`` over 1.1.0 reproduces the committed fixture exactly."""
    expected = _golden_lines()
    result = build_full_batch(seed=SEED_GOLD_B, max_events=len(expected))
    actual = [canonical_serialize(env) for env in result.envelopes]

    assert actual, "the GOLD-B replay produced no events"
    if len(actual) != len(expected):
        raise AssertionError(
            "GOLD-B event-count divergence: "
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
                f"GOLD-B byte divergence at line {line_no} (event_id={event_id}):\n"
                f"{_field_diff(exp, act)}\n"
                "Run `make golden-regen-b` only with an intentional, "
                "golden-rebaseline-labelled change (testing-strategy §6)."
            )


@pytest.mark.golden
def test_gold_b_fixture_actually_exercises_cdc() -> None:
    """The committed batch carries CDC rows — so byte-identity is over CDC + business.

    A determinism gate that never sees a CDC ``c``/``u`` would silently degrade to a
    business-only GOLD-A clone; assert the fixture contains the default-on CDC
    subjects (users/products/orders/inventory) with ``op`` ∈ {c, u}, so the gate
    actually proves CDC determinism (one mutation, two consistent views, ADR-0012)."""
    rows = [json.loads(line) for line in _golden_lines()]
    cdc_types = {r["event_type"] for r in rows if str(r["event_type"]).startswith("cdc.")}
    assert cdc_types, "GOLD-B fixture has no CDC rows — it is not exercising CDC"
    ops = {r["op"] for r in rows if str(r["event_type"]).startswith("cdc.")}
    assert ops <= {"c", "u", "d", "r"} and ops & {"c", "u"}, (
        f"GOLD-B CDC rows carry unexpected ops {ops!r}"
    )
    # The default-enabled CDC set is part of the manifest contract (1.1.0).
    document = full_ecommerce_document()
    default_on = {
        f"ecommerce.cdc.{e}"
        for e, cfg in ((document.get("cdc") or {}).get("entities", {})).items()
        if cfg.get("enabled_default")
    }
    seen = {f"ecommerce.{t}" for t in cdc_types}
    assert seen <= default_on, (
        f"GOLD-B emitted CDC for non-default-on entities: {seen - default_on}"
    )


@pytest.mark.golden
def test_gold_b_same_seed_is_stable_across_two_live_runs() -> None:
    """Two fresh in-process full-manifest runs at the pinned seed are byte-identical."""
    a = [
        canonical_serialize(e) for e in build_full_batch(seed=SEED_GOLD_B, max_events=600).envelopes
    ]
    b = [
        canonical_serialize(e) for e in build_full_batch(seed=SEED_GOLD_B, max_events=600).envelopes
    ]
    assert a and a == b


@pytest.mark.golden
def test_gold_b_different_seed_diverges() -> None:
    """A different seed yields a different canonical batch (no accidental fixity)."""
    a = [
        canonical_serialize(e) for e in build_full_batch(seed=SEED_GOLD_B, max_events=400).envelopes
    ]
    c = [
        canonical_serialize(e)
        for e in build_full_batch(seed=SEED_GOLD_B + 1, max_events=400).envelopes
    ]
    assert a != c
