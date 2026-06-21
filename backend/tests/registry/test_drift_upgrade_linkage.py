"""Drift ↔ upgrade linkage over the live registry (Exit #3, #4 drift half).

Phase-10 exit criterion #3: "drift-injected fields always resolve to a registered
version above the effective version; upgrade application rebuilds the drift menu
(post-upgrade drift uses v3 only)" + the CH-V07 arming gate ("arming fails before v2
exists, succeeds after").

Where ``tests/registry/test_drift_menu.py`` pins the DR-4 menu rebuild on a fixed
v1/v2/v3 trio and ``tests/chaos/test_chd_drift.py`` pins the engine-side injection,
this suite closes the registration loop the seed step drives: CH-V07 must flip from
*rejected* to *accepted* the moment v2 is **registered** (not merely as the effective
version changes), and it proves the permanent property — every field the drift menu
offers resolves to a registered version strictly above the stream's effective version —
directly against the real registry over the seeded evolutions.

Runs under the maintenance role (registers global v2/v3 via Flow 2, §5.2).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.management import call_command

from chaos.application.validation import ChaosPolicyInvalid, validate_drift_arming
from registry.application.drift_menu import build_drift_menu, drift_arming_eligible
from registry.application.services import get_version
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"
_DRIFT_ON = {"schema_drift": {"enabled": True, "rate": 0.2, "params": {}}}


def _seed() -> None:
    call_command("seed_schema_evolutions", stdout=io.StringIO())


# --- CH-V07: arming flips on the *registration* of the next version ------------


def test_chv07_arming_rejected_before_v2_registered_then_succeeds_after(
    published_ecommerce: Any,
) -> None:
    """CH-V07: enabling schema_drift on a v1-effective stream is rejected until v2 exists.

    Before the seed, ``ecommerce.order_placed`` has only v1 — effective v1 has no next
    version, so arming drift is a CH-V07 rejection. After the seed registers v2, the same
    arming call on the same v1-effective stream is accepted (the menu now has a target)."""
    # Before: only v1 registered → arming drift at effective v1 is rejected.
    with pytest.raises(ChaosPolicyInvalid) as exc:
        validate_drift_arming(
            resulting_config=_DRIFT_ON, effective={_SUBJECT: 1}, workspace_id=None
        )
    assert [e["code"] for e in exc.value.errors] == ["CH-V07"]
    assert not drift_arming_eligible(effective={_SUBJECT: 1}, workspace_id=None)

    # Register v2/v3 via the seed (Flow 2).
    _seed()

    # After: the same v1-effective arming is now accepted (v2 is a valid drift target).
    assert drift_arming_eligible(effective={_SUBJECT: 1}, workspace_id=None)
    validate_drift_arming(  # no exception
        resulting_config=_DRIFT_ON, effective={_SUBJECT: 1}, workspace_id=None
    )


def test_chv07_arming_rejected_at_highest_registered_version(
    published_ecommerce: Any,
) -> None:
    """CH-V07: at the highest registered version (v3) there is no next version → rejected."""
    _seed()
    with pytest.raises(ChaosPolicyInvalid) as exc:
        validate_drift_arming(
            resulting_config=_DRIFT_ON, effective={_SUBJECT: 3}, workspace_id=None
        )
    assert [e["code"] for e in exc.value.errors] == ["CH-V07"]


# --- DR-4: the menu rebuilds as the effective version advances -----------------


def test_dr4_menu_rebuilds_state_then_city(published_ecommerce: Any) -> None:
    """DR-4: effective v1 offers only shipping_state; effective v2 rebuilds to shipping_city."""
    _seed()
    v1_menu = build_drift_menu(effective={_SUBJECT: 1}, workspace_id=None)
    assert {f["path"] for f in v1_menu[_SUBJECT].added_fields} == {"shipping_state"}
    assert (v1_menu[_SUBJECT].from_version, v1_menu[_SUBJECT].to_version) == (1, 2)

    v2_menu = build_drift_menu(effective={_SUBJECT: 2}, workspace_id=None)
    assert {f["path"] for f in v2_menu[_SUBJECT].added_fields} == {"shipping_city"}
    assert (v2_menu[_SUBJECT].from_version, v2_menu[_SUBJECT].to_version) == (2, 3)

    # Effective v3 (highest) → ineligible, the menu is empty.
    assert build_drift_menu(effective={_SUBJECT: 3}, workspace_id=None) == {}


# --- the permanent property: every menu field resolves to a registered v > effective ---


@pytest.mark.parametrize("effective", [1, 2])
def test_every_drift_field_resolves_to_a_registered_version_above_effective(
    published_ecommerce: Any, effective: int
) -> None:
    """DR-6 (permanent): every field the menu offers is in a registered version > effective.

    The drift menu must never invent a field — every ``{path, fragment}`` it offers comes
    from the next REGISTERED version, which is strictly above the stream's effective
    version, and that version's stored document actually contains the field (the property
    the chaos stage's injection relies on, §5.5 / INV-CHA-3)."""
    _seed()
    menu = build_drift_menu(effective={_SUBJECT: effective}, workspace_id=None)
    entry = menu[_SUBJECT]
    # The target version is registered and strictly above the effective version.
    assert entry.to_version > effective
    target = get_version(_SUBJECT, entry.to_version, workspace_id=None)
    assert target is not None
    target_props = target.json_schema["properties"]
    # Every offered field is a real property of the target version's stored document …
    for field in entry.added_fields:
        leaf = field["path"].rsplit("/", 1)[-1]
        assert leaf in target_props
    # … and is NOT yet present in the effective version (it is genuinely "next").
    effective_doc = get_version(_SUBJECT, effective, workspace_id=None)
    assert effective_doc is not None
    for field in entry.added_fields:
        leaf = field["path"].rsplit("/", 1)[-1]
        assert leaf not in effective_doc.json_schema["properties"]


def test_cdc_subject_is_never_a_drift_target(published_ecommerce: Any) -> None:
    """A cdc.* subject never appears in the menu even when it has a next version (R-CDC-6).

    (Defensive: REG-U006/C012 forbid evolving cdc.* through Flow 2, but if a CDC subject
    ever has multiple versions, drift must still skip it.)"""
    _seed()
    # Manufacture a second cdc.users version directly (bypassing Flow 2's C012 gate).
    cdc = Subject.objects.get(subject="ecommerce.cdc.users")
    base = SchemaVersion.objects.filter(subject=cdc).order_by("-version").first()
    assert base is not None
    SchemaVersion.objects.create(
        subject=cdc,
        workspace_id=None,
        version=base.version + 1,
        json_schema={**base.json_schema},
        fingerprint="cdc-drift-probe",
    )
    menu = build_drift_menu(
        effective={_SUBJECT: 1, "ecommerce.cdc.users": base.version}, workspace_id=None
    )
    assert "ecommerce.cdc.users" not in menu
