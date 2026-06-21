"""CHD-6 — schema_drift injects only registered next-version fields (§10.2, §5.5).

Phase-9 exit criterion #7 / Phase-10 gate (PR): drift fields ⊆ the registered next
version's field set, never written into envelope fields or CDC ``before`` images
(INV-CHA-3, R-CDC-6). The per-stage mechanics are unit-covered in
``dataforge_engine/chaos/tests/test_modes_drift_order.py``; this is the CHD binding
in the cross-app chaos suite, asserted over the deterministic projection (the v2
drift menu arms ``shipping_state`` for every fixture subject).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from dataforge_engine.chaos import default_policy
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.chaos.stages.schema_drift import SchemaDriftStage
from dataforge_engine.chaos.tests.fixtures import (
    FakeDriftMenu,
    FakeRegistryView,
    make_cdc_envelope,
    make_context,
)
from dataforge_engine.envelope import DELIVERED_FIELD_ORDER
from tests.chaos.projection import run_projection

pytestmark = pytest.mark.chaos

N = 5000
_ENVELOPE_FIELDS = [f for f in DELIVERED_FIELD_ORDER if f != "payload"]
_V2_FIELD = {"path": "shipping_state", "fragment": {"type": "string"}}


def _drift_only_policy(rate: float = 0.20) -> Any:
    policy = default_policy()
    policy["schema_drift"]["enabled"] = True
    policy["schema_drift"]["rate"] = rate
    return policy


def test_chd6_drift_fields_subset_of_registered_next_version() -> None:
    """CHD-6: every drifted event gains ONLY the registered v2 field, schema_ref kept."""
    proj = run_projection(_drift_only_policy(), n=N)
    touched = [
        cast("dict[str, Any]", dict(e)) for e in proj.delivered if not e["_df"]["canonical"]
    ]
    assert touched, "no drift applied"
    for env in touched:
        detail = cast(dict[str, Any], env["_df"]["chaos"]["schema_drift"])
        assert detail["from_version"] == 1
        assert detail["to_version"] == 2
        added = {f["path"] for f in detail["fields_added"]}
        assert added == {"shipping_state"}  # subset of the registered next version's set
        assert "shipping_state" in env["payload"]
        # The envelope's schema_ref keeps the stream's EFFECTIVE version (§5.5).
        assert env["schema_ref"]["version"] == 1


def test_chd6_drift_never_touches_envelope_fields() -> None:
    """CHD-6: drift only grows the payload — envelope fields are byte-identical."""
    proj = run_projection(_drift_only_policy(), n=N)
    by_seq = {e["sequence_no"]: cast("dict[str, Any]", dict(e)) for e in proj.ledger}
    for raw in (e for e in proj.delivered if not e["_df"]["canonical"]):
        env = cast("dict[str, Any]", dict(raw))
        orig = by_seq[env["sequence_no"]]
        for field in _ENVELOPE_FIELDS:
            assert env[field] == orig[field]


def test_chd6_drift_never_writes_cdc_before_image() -> None:
    """CHD-6 / R-CDC-6: CDC drift adds to ``after`` only, never ``before``."""
    rec = InMemoryRecorder()
    menu = FakeRegistryView({"cdc.users": FakeDriftMenu(1, 2, [dict(_V2_FIELD)])})
    ctx = make_context(rec, registry_view=menu)
    ctx.mode_config = {
        "enabled": True,
        "rate": 0.5,
        "params": {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]},
    }
    batch = [
        make_cdc_envelope(i, op="u", before={"email": "a@x"}, after={"email": "b@x"})
        for i in range(1, 300)
    ]
    out = SchemaDriftStage().process(batch, ctx)
    touched = [e for e in out if not e["_df"]["canonical"]]
    assert touched
    for env in touched:
        payload = cast(dict[str, Any], env["payload"])
        assert "shipping_state" in payload["after"]
        assert "shipping_state" not in payload["before"]


def test_chd6_drift_noop_without_registered_next_version() -> None:
    """CHD-6: with no next version, drift can never invent a field (CH-V07)."""
    rec = InMemoryRecorder()
    ctx = make_context(rec, registry_view=FakeRegistryView({}))
    ctx.mode_config = {
        "enabled": True,
        "rate": 0.5,
        "params": {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]},
    }
    from dataforge_engine.chaos.tests.fixtures import make_batch

    out = SchemaDriftStage().process(make_batch(N), ctx)
    assert all(e["_df"]["canonical"] for e in out)
    assert len(rec.records) == 0


# -- DR-6: the v2/v3 trio (schema-registry §11 worked linkage) ---------------------
# "every drift-injected field resolves to a registered version > effective" exercised
# over the §9 evolution trio: effective at v1 drifts ONLY shipping_state (v2 is the
# next version — one step, not two); after the stream upgrades to v2, the menu rebuild
# makes shipping_city (v3) the injectable field. The drift stage reads only the menu
# the registry_view port supplies (DR-1), so the trio is expressed as the two menus a
# v1-effective and a v2-effective stream see — the runner's DR-4 rebuild swaps between
# them when an upgrade applies (the menu-level rebuild is pinned in
# tests/registry/test_drift_menu.py against the real registry).

_V3_CITY = {"path": "shipping_city", "fragment": {"type": "string"}}


def _drift_trio_context(rec: InMemoryRecorder, menu: FakeDriftMenu) -> Any:
    ctx = make_context(rec, registry_view=FakeRegistryView({"shop.order_placed": menu}))
    ctx.mode_config = {
        "enabled": True,
        "rate": 0.5,
        "params": {"subjects": ["*"], "fields": ["*"], "event_types": ["*"]},
    }
    return ctx


def test_dr6_effective_v1_drifts_only_shipping_state() -> None:
    """DR-6: a stream effective at v1 drifts ONLY shipping_state (v2 — next, not v3)."""
    from dataforge_engine.chaos.tests.fixtures import make_batch

    rec = InMemoryRecorder()
    ctx = _drift_trio_context(rec, FakeDriftMenu(1, 2, [dict(_V2_FIELD)]))
    out = SchemaDriftStage().process(make_batch(N), ctx)
    touched = [cast("dict[str, Any]", dict(e)) for e in out if not e["_df"]["canonical"]]
    assert touched, "no drift applied"
    for env in touched:
        detail = cast(dict[str, Any], env["_df"]["chaos"]["schema_drift"])
        assert (detail["from_version"], detail["to_version"]) == (1, 2)
        added = {f["path"] for f in detail["fields_added"]}
        assert added == {"shipping_state"}  # shipping_city (v3) is NOT injectable yet
        assert "shipping_state" in env["payload"]
        assert "shipping_city" not in env["payload"]
        assert env["schema_ref"]["version"] == 1  # schema_ref keeps the effective version


def test_dr6_after_upgrade_to_v2_drifts_only_shipping_city() -> None:
    """DR-6: post-upgrade (effective v2) the rebuilt menu drifts ONLY shipping_city (v3)."""
    from dataforge_engine.chaos.tests.fixtures import make_batch

    rec = InMemoryRecorder()
    ctx = _drift_trio_context(rec, FakeDriftMenu(2, 3, [dict(_V3_CITY)]))
    out = SchemaDriftStage().process(make_batch(N), ctx)
    touched = [cast("dict[str, Any]", dict(e)) for e in out if not e["_df"]["canonical"]]
    assert touched, "no drift applied"
    for env in touched:
        detail = cast(dict[str, Any], env["_df"]["chaos"]["schema_drift"])
        assert (detail["from_version"], detail["to_version"]) == (2, 3)
        added = {f["path"] for f in detail["fields_added"]}
        assert added == {"shipping_city"}  # shipping_state is now effective, not injected
        assert "shipping_city" in env["payload"]
        assert "shipping_state" not in env["payload"]
