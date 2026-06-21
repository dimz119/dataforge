"""Per-mode tests for chaos modes 5-6 (chaos-engine §5.5 schema_drift, §5.6 oo).

Covers: drift injects only registered-next-version fields against a test-provided
v2 menu (DR-1); drift NEVER touches envelope fields or CDC ``before`` images
(R-CDC-6); drift is a no-op when no next version exists (DR-3); ``schema_ref`` is
kept at the effective version; out_of_order displaces only within the window and
records the displacement (§5.6.5); both are deterministic (INV-CHA-2).
"""

from __future__ import annotations

from typing import Any, cast

from dataforge_engine.chaos import ModeConfig
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.chaos.stages.out_of_order import OutOfOrderStage
from dataforge_engine.chaos.stages.schema_drift import SchemaDriftStage
from dataforge_engine.envelope import DELIVERED_FIELD_ORDER

from .fixtures import (
    FakeDriftMenu,
    FakeRegistryView,
    FakeVirtualClock,
    base_epoch_ms,
    make_batch,
    make_cdc_envelope,
    make_context,
)

N = 5000
ENVELOPE_FIELDS = [f for f in DELIVERED_FIELD_ORDER if f != "payload"]

# v2 menu: order_placed v1 → v2 adds optional ``shipping_state`` (§5.5 worked ex).
_V2_FIELD = {"path": "shipping_state", "fragment": {"type": "string"}}
_ORDER_SUBJECT = "shop.order_placed"


def _cfg(rate: float, params: dict[str, Any]) -> ModeConfig:
    return {"enabled": True, "rate": rate, "params": params}


def _order_menu() -> FakeRegistryView:
    return FakeRegistryView(
        {_ORDER_SUBJECT: FakeDriftMenu(1, 2, [dict(_V2_FIELD)])}
    )


# --- schema_drift (§5.5) ---------------------------------------------------


def test_drift_injects_only_registered_next_version_fields() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec, registry_view=_order_menu())
    ctx.mode_config = _cfg(0.20, {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]})
    out = SchemaDriftStage().process(make_batch(N), ctx)
    touched = [e for e in out if not e["_df"]["canonical"]]
    assert abs(len(touched) / N - 0.20) < 0.02
    assert len(rec.records) == len(touched)
    for e in touched:
        ed = cast(dict[str, Any], e)
        payload = ed["payload"]
        assert "shipping_state" in payload  # the only registered v2 field
        assert isinstance(payload["shipping_state"], str)
        detail = ed["_df"]["chaos"]["schema_drift"]
        assert detail["from_version"] == 1
        assert detail["to_version"] == 2
        assert detail["fields_added"][0]["path"] == "shipping_state"
        # schema_ref keeps the stream's EFFECTIVE version (§5.5).
        assert ed["schema_ref"]["version"] == 1


def test_drift_never_touches_envelope_fields() -> None:
    ctx = make_context(registry_view=_order_menu())
    ctx.mode_config = _cfg(0.20, {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]})
    batch = make_batch(N)
    by_seq = {e["sequence_no"]: dict(e) for e in batch}
    out = SchemaDriftStage().process(batch, ctx)
    for e in (e for e in out if not e["_df"]["canonical"]):
        orig = by_seq[e["sequence_no"]]
        ed = cast(dict[str, Any], e)
        for field in ENVELOPE_FIELDS:
            assert ed[field] == orig[field]  # only payload grew


def test_drift_never_touches_cdc_before_image() -> None:
    rec = InMemoryRecorder()
    menu = FakeRegistryView({"cdc.users": FakeDriftMenu(1, 2, [dict(_V2_FIELD)])})
    ctx = make_context(rec, registry_view=menu)
    ctx.mode_config = _cfg(0.5, {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]})
    batch = [
        make_cdc_envelope(i, op="u", before={"email": "a@x"}, after={"email": "b@x"})
        for i in range(1, 200)
    ]
    out = SchemaDriftStage().process(batch, ctx)
    touched = [e for e in out if not e["_df"]["canonical"]]
    assert touched  # drift armed against cdc.users v2
    for e in touched:
        payload = cast(dict[str, Any], e["payload"])
        assert "shipping_state" in payload["after"]  # added to after
        assert "shipping_state" not in payload["before"]  # NEVER before (R-CDC-6)


def test_drift_cdc_delete_is_noop_no_before_drift() -> None:
    menu = FakeRegistryView({"cdc.users": FakeDriftMenu(1, 2, [dict(_V2_FIELD)])})
    ctx = make_context(registry_view=menu)
    ctx.mode_config = _cfg(0.5, {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]})
    batch = [make_cdc_envelope(i, op="d", before={"email": "a@x"}, after=None)
             for i in range(1, 200)]
    out = SchemaDriftStage().process(batch, ctx)
    # after is null on a delete ⇒ nothing to drift, before untouched.
    assert all(e["_df"]["canonical"] for e in out)


def test_drift_noop_when_no_next_version() -> None:
    rec = InMemoryRecorder()
    ctx = make_context(rec, registry_view=FakeRegistryView({}))  # empty menu
    ctx.mode_config = _cfg(0.5, {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]})
    batch = make_batch(N)
    snapshot = [dict(e) for e in batch]
    out = SchemaDriftStage().process(batch, ctx)
    assert all(e["_df"]["canonical"] for e in out)  # nothing armed
    assert len(rec.records) == 0
    assert [dict(e) for e in out] == snapshot  # identity transform


def test_drift_deterministic() -> None:
    ctx_a = make_context(registry_view=_order_menu())
    ctx_b = make_context(registry_view=_order_menu())
    p = {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]}
    ctx_a.mode_config = _cfg(0.2, p)
    ctx_b.mode_config = _cfg(0.2, p)
    out_a = SchemaDriftStage().process(make_batch(N), ctx_a)
    out_b = SchemaDriftStage().process(make_batch(N), ctx_b)
    assert [e["payload"] for e in out_a] == [e["payload"] for e in out_b]


# --- out_of_order (§5.6) ---------------------------------------------------


def test_out_of_order_displaces_within_window_and_records() -> None:
    rec = InMemoryRecorder()
    # Window wide enough that the whole batch is one window (seqs are 1ms apart).
    clock = FakeVirtualClock(base_epoch_ms())
    ctx = make_context(rec, virtual_clock=clock)
    ctx.mode_config = _cfg(0.3, {"window": "PT60S", "event_types": ["*"]})
    batch = make_batch(200)
    canonical_seqs = [e["sequence_no"] for e in batch]
    out = OutOfOrderStage().process(batch, ctx)
    # Same multiset of events (a shuffle, never a drop/add).
    assert sorted(e["sequence_no"] for e in out) == sorted(canonical_seqs)
    out_seqs = [e["sequence_no"] for e in out]
    assert out_seqs != canonical_seqs  # something moved
    displaced = [e for e in out if not e["_df"]["canonical"]]
    assert len(rec.records) == len(displaced)
    for e in displaced:
        detail = cast(dict[str, Any], e)["_df"]["chaos"]["out_of_order"]
        assert detail["window_simulated_ms"] == 60000
        from_pos = detail["displaced_from_position"]
        # The recorded source position differs from its delivered position.
        assert out_seqs.index(e["sequence_no"]) != from_pos


def test_out_of_order_does_not_cross_windows() -> None:
    # Tiny 1ms window: every consecutive 1ms-spaced event is its own window, so a
    # single-member window can never permute → batch is unchanged.
    clock = FakeVirtualClock(base_epoch_ms())
    ctx = make_context(virtual_clock=clock)
    ctx.mode_config = _cfg(0.5, {"window": "PT1S", "event_types": ["*"]})
    batch = make_batch(50)
    # Spread events ≥ 1s apart so each lands alone in its own 1s window.
    for i, e in enumerate(batch):
        from datetime import UTC, datetime, timedelta

        moment = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC) + timedelta(seconds=i * 2)
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        e["occurred_at"] = stamp
    out = OutOfOrderStage().process(batch, ctx)
    assert [e["sequence_no"] for e in out] == [e["sequence_no"] for e in batch]
    assert all(e["_df"]["canonical"] for e in out)  # no displacement possible


def test_out_of_order_deterministic() -> None:
    clock = FakeVirtualClock(base_epoch_ms())
    ctx_a = make_context(virtual_clock=clock)
    ctx_b = make_context(virtual_clock=FakeVirtualClock(base_epoch_ms()))
    ctx_a.mode_config = _cfg(0.3, {"window": "PT60S", "event_types": ["*"]})
    ctx_b.mode_config = _cfg(0.3, {"window": "PT60S", "event_types": ["*"]})
    out_a = OutOfOrderStage().process(make_batch(200), ctx_a)
    out_b = OutOfOrderStage().process(make_batch(200), ctx_b)
    assert [e["sequence_no"] for e in out_a] == [e["sequence_no"] for e in out_b]
