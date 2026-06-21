"""GOLD-C — byte-identical replay of the all-7-modes chaos projection (§10.1, CHD-2).

Phase-9 exit criterion #2 (permanent): the pure chaos pipeline, run at
``SEED_GOLD_C`` over a 5,000-event canonical batch with **all seven modes
enabled** (the §11 GOLD-C determinism unit), must reproduce the committed
``delivered.jsonl.gz`` + ``injections.jsonl.gz`` **byte-for-byte** — the
post-chaos delivery stream (survivors + duplicate copies + corrupted/nulled/
drifted payloads + the out-of-order shuffle) and the sorted CHD-1 injection
projection (wall-clock artifacts dropped).

This proves chaos determinism survives composition: same ``(seed, chaos config)``
⇒ identical chaos decisions (INV-CHA-2), including ordering effects, over every
mode at once. CI never regenerates the fixture; ``uv run python -m
tests.golden.regen_gold_c`` is the only (local, ``golden-rebaseline``-labelled)
way. Pure engine + ports — the fast golden lane (no Postgres, no broker).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tests.golden.harness_gold_c import (
    GOLD_C_EVENTS,
    config_sha256,
    delivered_lines,
    injection_lines,
)
from tests.seeds import SEED_GOLD_C

_FIXTURE_DIR = Path(__file__).resolve().parent / "chaos" / "gold-c-5k"
_DELIVERED_FILE = _FIXTURE_DIR / "delivered.jsonl.gz"
_INJECTIONS_FILE = _FIXTURE_DIR / "injections.jsonl.gz"
_META_FILE = _FIXTURE_DIR / "meta.json"


def _gz_lines(path: Path) -> list[bytes]:
    with gzip.open(path, "rb") as fh:
        blob = fh.read()
    return [line for line in blob.split(b"\n") if line]


def _meta() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_META_FILE.read_text(encoding="utf-8"))
    return data


def _assert_byte_identity(expected: list[bytes], actual: list[bytes], label: str) -> None:
    assert actual, f"the GOLD-C {label} replay produced no lines"
    if len(actual) != len(expected):
        raise AssertionError(
            f"GOLD-C {label} count divergence: committed={len(expected)} "
            f"replayed={len(actual)} (a count change is a determinism regression "
            "unless golden-rebaselined)"
        )
    for line_no, (exp, act) in enumerate(zip(expected, actual, strict=True)):
        if exp != act:
            raise AssertionError(
                f"GOLD-C {label} byte divergence at line {line_no}:\n"
                f"  committed: {exp[:200]!r}\n  replayed:  {act[:200]!r}\n"
                "Run `uv run python -m tests.golden.regen_gold_c` only with an "
                "intentional, golden-rebaseline-labelled change (testing-strategy §6)."
            )


@pytest.mark.golden
def test_gold_c_meta_is_well_formed() -> None:
    """The fixture metadata records the PIN-1 determinism unit for all-7-modes."""
    meta = _meta()
    assert meta["seed"] == SEED_GOLD_C
    assert meta["modes"] == "all-7"
    assert meta["canonical_events"] == GOLD_C_EVENTS
    assert meta["config_sha256"] == config_sha256()  # config drift fails here
    assert meta["envelope_version"] == "1.0"
    assert meta["delivered_count"] == len(_gz_lines(_DELIVERED_FILE))
    assert meta["injection_count"] == len(_gz_lines(_INJECTIONS_FILE))


@pytest.mark.golden
def test_gold_c_delivered_replays_byte_identically() -> None:
    """Replaying SEED_GOLD_C reproduces the committed delivered stream exactly (CHD-2)."""
    _assert_byte_identity(_gz_lines(_DELIVERED_FILE), delivered_lines(), "delivered")


@pytest.mark.golden
def test_gold_c_injections_replay_byte_identically() -> None:
    """The committed injection projection is reproduced exactly (CHD-1 byte-identity)."""
    _assert_byte_identity(_gz_lines(_INJECTIONS_FILE), injection_lines(), "injections")


@pytest.mark.golden
def test_gold_c_two_live_runs_are_byte_identical() -> None:
    """Two fresh in-process all-7-modes runs at the pinned seed agree (no fixture)."""
    assert delivered_lines() == delivered_lines()
    assert injection_lines() == injection_lines()


@pytest.mark.golden
def test_gold_c_fixture_exercises_every_mode() -> None:
    """The committed injection projection contains every one of the seven modes.

    A determinism gate that silently lost a mode would degrade to a partial-chaos
    fixture; assert all seven ChaosMode identifiers appear in the projection so the
    gate genuinely proves all-7-modes determinism."""
    rows = [json.loads(line) for line in _gz_lines(_INJECTIONS_FILE)]
    modes_seen = {row[0] for row in rows}  # projection row[0] == mode
    assert modes_seen == {
        "missing",
        "duplicates",
        "corrupted_values",
        "nulls",
        "schema_drift",
        "out_of_order",
        "late_arriving",
    }, f"GOLD-C is missing modes: {modes_seen}"
