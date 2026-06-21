"""Per-mode behaviour tests for chaos modes 1-4 (chaos-engine §5.1-5.4).

Covers: configured rate realises ~that rate; same (seed, config) ⇒ identical
output (determinism); one InjectionRecord per affected event (record-before-effect,
INV-CHA-4); duplicates byte-identical; nulls/corrupted PAYLOAD-only (never
envelope fields, CR-6); the ledger/input batch is never mutated (CHD-4/5).
"""

from __future__ import annotations

import copy
from typing import Any, cast

from dataforge_engine.chaos import ChaosPipeline, ModeConfig, default_policy
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.chaos.stages.corrupted_values import CorruptedValuesStage
from dataforge_engine.chaos.stages.duplicates import DuplicatesStage
from dataforge_engine.chaos.stages.missing import MissingStage
from dataforge_engine.chaos.stages.nulls import NullsStage
from dataforge_engine.envelope import DELIVERED_FIELD_ORDER

from .fixtures import make_batch, make_context

N = 5000
ENVELOPE_FIELDS = [f for f in DELIVERED_FIELD_ORDER if f != "payload"]

# Shared per-mode params (kept short to respect the 100-col line limit).
_MISSING_P: dict[str, Any] = {"event_types": ["*"]}
_DUP_P: dict[str, Any] = {
    "copies": [{"count": 1, "weight": 1.0}],
    "spacing": {"mode": "adjacent"},
    "event_types": ["*"],
}
_CORRUPT_P: dict[str, Any] = {
    "fields": ["*"],
    "kinds": ["*"],
    "max_fields_per_event": 1,
    "event_types": ["*"],
}
_NULLS_P: dict[str, Any] = {
    "fields": ["*"],
    "include_nullable": False,
    "max_fields_per_event": 1,
    "event_types": ["*"],
}


def _cfg(rate: float, params: dict[str, Any]) -> ModeConfig:
    return {"enabled": True, "rate": rate, "params": params}


# --- missing (§5.1) --------------------------------------------------------


def test_missing_realises_configured_rate() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec)
    ctx.mode_config = _cfg(0.05, _MISSING_P)
    out = MissingStage().process(make_batch(N), ctx)
    dropped = N - len(out)
    assert abs(dropped / N - 0.05) < 0.01
    assert len(rec.records) == dropped  # one record per suppressed event
    assert all(r["mode"] == "missing" for r in rec.records)
    assert all(r["details"] == {} for r in rec.records)


def test_missing_deterministic() -> None:
    ctx_a, ctx_b = make_context(), make_context()
    ctx_a.mode_config = _cfg(0.1, _MISSING_P)
    ctx_b.mode_config = _cfg(0.1, _MISSING_P)
    out_a = MissingStage().process(make_batch(N), ctx_a)
    out_b = MissingStage().process(make_batch(N), ctx_b)
    assert [e["event_id"] for e in out_a] == [e["event_id"] for e in out_b]


# --- duplicates (§5.2) -----------------------------------------------------


def test_duplicates_realises_rate_and_byte_identical_copies() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec)
    ctx.mode_config = _cfg(0.05, _DUP_P)
    batch = make_batch(N)
    out = DuplicatesStage().process(batch, ctx)
    extra = len(out) - N
    assert abs(extra / N - 0.05) < 0.01
    assert len(rec.records) == extra  # one record per duplicated event, 1 copy each
    # Copies are byte-identical except the _df block.
    by_id: dict[str, list[dict[str, Any]]] = {}
    for e in out:
        by_id.setdefault(e["event_id"], []).append(dict(e))
    pairs = [v for v in by_id.values() if len(v) == 2]
    assert pairs
    for original, copy_ in pairs:
        delivered_orig = {k: v for k, v in original.items() if k != "_df"}
        delivered_copy = {k: v for k, v in copy_.items() if k != "_df"}
        assert delivered_orig == delivered_copy  # byte-identical delivered envelope
        assert copy_["_df"]["chaos"]["duplicates"]["duplicate_index"] == 1
        assert copy_["_df"]["canonical"] is False


def test_duplicates_deterministic() -> None:
    ctx_a, ctx_b = make_context(), make_context()
    ctx_a.mode_config = _cfg(0.08, _DUP_P)
    ctx_b.mode_config = _cfg(0.08, _DUP_P)
    out_a = DuplicatesStage().process(make_batch(N), ctx_a)
    out_b = DuplicatesStage().process(make_batch(N), ctx_b)
    assert [e["event_id"] for e in out_a] == [e["event_id"] for e in out_b]


# --- corrupted_values (§5.3) ----------------------------------------------


def test_corrupted_values_realises_rate_payload_only() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec)
    ctx.mode_config = _cfg(0.05, _CORRUPT_P)
    batch = make_batch(N)
    snapshot = copy.deepcopy([dict(e) for e in batch])
    out = CorruptedValuesStage().process(batch, ctx)
    touched = [e for e in out if not e["_df"]["canonical"]]
    assert abs(len(touched) / N - 0.05) < 0.01
    assert len(rec.records) == len(touched)
    # PAYLOAD-only: every envelope field byte-identical to canonical (CR-6).
    by_seq = {dict(s)["sequence_no"]: dict(s) for s in snapshot}
    for e in touched:
        ed = cast(dict[str, Any], e)
        orig = by_seq[e["sequence_no"]]
        for field in ENVELOPE_FIELDS:
            assert ed[field] == orig[field]
        assert e["payload"] != orig["payload"]  # something in payload changed
    # Input batch (the ledger source) is never mutated (CHD-4/5).
    assert [dict(e) for e in batch] == snapshot


def test_corrupted_values_deterministic() -> None:
    ctx_a, ctx_b = make_context(), make_context()
    ctx_a.mode_config = _cfg(0.1, _CORRUPT_P)
    ctx_b.mode_config = _cfg(0.1, _CORRUPT_P)
    out_a = CorruptedValuesStage().process(make_batch(N), ctx_a)
    out_b = CorruptedValuesStage().process(make_batch(N), ctx_b)
    assert [e["payload"] for e in out_a] == [e["payload"] for e in out_b]


# --- nulls (§5.4) ----------------------------------------------------------


def test_nulls_realises_rate_payload_only_never_envelope() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec)
    ctx.mode_config = _cfg(0.05, _NULLS_P)
    batch = make_batch(N)
    snapshot = {dict(e)["sequence_no"]: copy.deepcopy(dict(e)) for e in batch}
    out = NullsStage().process(batch, ctx)
    touched = [e for e in out if not e["_df"]["canonical"]]
    assert abs(len(touched) / N - 0.05) < 0.01
    assert len(rec.records) == len(touched)
    for e in touched:
        ed = cast(dict[str, Any], e)
        orig = snapshot[e["sequence_no"]]
        for field in ENVELOPE_FIELDS:
            assert ed[field] == orig[field]
        nulled = ed["_df"]["chaos"]["nulls"]["mutations"]
        assert nulled and all(isinstance(m, dict) for m in nulled)


def test_nulls_deterministic() -> None:
    ctx_a, ctx_b = make_context(), make_context()
    ctx_a.mode_config = _cfg(0.1, _NULLS_P)
    ctx_b.mode_config = _cfg(0.1, _NULLS_P)
    out_a = NullsStage().process(make_batch(N), ctx_a)
    out_b = NullsStage().process(make_batch(N), ctx_b)
    assert [e["payload"] for e in out_a] == [e["payload"] for e in out_b]


# --- pipeline composition (§2.3) ------------------------------------------


def test_full_pipeline_same_seed_identical_records() -> None:
    policy = default_policy()
    for mode in ("missing", "duplicates", "corrupted_values", "nulls"):
        policy[mode]["enabled"] = True
        policy[mode]["rate"] = 0.05
    rec_a, rec_b = InMemoryRecorder(), InMemoryRecorder()
    out_a = ChaosPipeline(policy).transform(make_batch(N), make_context(rec_a))
    out_b = ChaosPipeline(policy).transform(make_batch(N), make_context(rec_b))
    assert [e["event_id"] for e in out_a] == [e["event_id"] for e in out_b]
    ids_a = sorted(r["injection_id"] for r in rec_a.records)
    ids_b = sorted(r["injection_id"] for r in rec_b.records)
    assert ids_a == ids_b  # identical injection record sets (CHD-1/2)
