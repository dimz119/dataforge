"""DR-4 — the drift field menu rebuilds when an upgrade raises the effective version.

schema-registry §11 DR-1/DR-4 + the §9 v2/v3 trio. The menu
(:func:`registry.application.drift_menu.build_drift_menu`) keys off the stream's
CURRENT effective version, so applying a mid-stream upgrade (which raises the
effective value the caller passes in) automatically drops the now-effective
version's fields on the next refresh — a subject upgraded to its highest registered
version becomes ineligible (CH-V07 / DR-3). These tests pin that automatic rebuild
on a v1/v2/v3 trio for ``ecommerce.order_placed`` (v2 adds ``shipping_state``, v3
adds ``shipping_city``) without a running stream — the menu is a pure function of
(effective map, registry).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from registry.application.drift_menu import (
    build_drift_menu,
    drift_arming_eligible,
)
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"
_V1_REQUIRED = [
    "order_id",
    "user_id",
    "items",
    "currency",
    "subtotal",
    "shipping_fee",
    "total",
    "shipping_country",
]


def _doc(version: int, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A closed ``order_placed`` document at ``version`` (the §9 trio, abbreviated)."""
    properties: dict[str, Any] = {
        "order_id": {"type": "string"},
        "user_id": {"type": "string"},
        "items": {"type": "array", "items": {"type": "object"}},
        "currency": {"const": "USD"},
        "subtotal": {"type": "string"},
        "shipping_fee": {"type": "string"},
        "total": {"type": "string"},
        "shipping_country": {"type": "string"},
    }
    properties.update(extra or {})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{_SUBJECT} v{version}",
        "type": "object",
        "additionalProperties": False,
        "required": _V1_REQUIRED,
        "properties": properties,
    }


@pytest.fixture
def trio(db: Any) -> None:
    """Register a global v1/v2/v3 trio for ``ecommerce.order_placed`` (additive, §9)."""
    subject = Subject.objects.create(subject=_SUBJECT, scenario_id=uuid4(), workspace_id=None)
    SchemaVersion.objects.create(
        subject=subject, workspace_id=None, version=1, json_schema=_doc(1), fingerprint="fp1"
    )
    SchemaVersion.objects.create(
        subject=subject,
        workspace_id=None,
        version=2,
        json_schema=_doc(
            2, extra={"shipping_state": {"type": "string", "x-df-binding": {"from": "a.s"}}}
        ),
        fingerprint="fp2",
    )
    SchemaVersion.objects.create(
        subject=subject,
        workspace_id=None,
        version=3,
        json_schema=_doc(
            3,
            extra={
                "shipping_state": {"type": "string", "x-df-binding": {"from": "a.s"}},
                "shipping_city": {"type": "string", "x-df-binding": {"from": "a.c"}},
            },
        ),
        fingerprint="fp3",
    )


def test_dr4_effective_v1_drifts_only_shipping_state(trio: None) -> None:
    """Effective at v1: the menu targets v2 and offers ONLY shipping_state (next, not latest)."""
    menu = build_drift_menu(effective={_SUBJECT: 1}, workspace_id=None)
    assert set(menu) == {_SUBJECT}
    entry = menu[_SUBJECT]
    assert (entry.from_version, entry.to_version) == (1, 2)
    paths = {f["path"] for f in entry.added_fields}
    assert paths == {"shipping_state"}  # v3's shipping_city is NOT injectable yet
    # The fragment is carried intact for type-directed synthesis (DR-2), binding included.
    frag = entry.added_fields[0]["fragment"]
    assert frag["type"] == "string"
    assert frag["x-df-binding"] == {"from": "a.s"}


def test_dr4_after_upgrade_to_v2_menu_rebuilds_to_shipping_city(trio: None) -> None:
    """DR-4: raising effective to v2 (an applied upgrade) rebuilds the menu to v3/shipping_city."""
    menu = build_drift_menu(effective={_SUBJECT: 2}, workspace_id=None)
    entry = menu[_SUBJECT]
    assert (entry.from_version, entry.to_version) == (2, 3)
    paths = {f["path"] for f in entry.added_fields}
    assert paths == {"shipping_city"}  # shipping_state is now effective, no longer injectable


def test_dr4_effective_at_highest_version_is_ineligible(trio: None) -> None:
    """DR-4: a subject upgraded to its highest registered version drops out of the menu."""
    menu = build_drift_menu(effective={_SUBJECT: 3}, workspace_id=None)
    assert menu == {}
    assert drift_arming_eligible(effective={_SUBJECT: 3}, workspace_id=None) is False


def test_dr4_ceiling_default_is_effective_plus_one(trio: None) -> None:
    """Ceiling default effective+1: drift never reaches past the immediate next version."""
    # Even with v3 registered, effective v1 only ever sees v2 (the next), never v3.
    menu = build_drift_menu(effective={_SUBJECT: 1}, workspace_id=None)
    assert menu[_SUBJECT].to_version == 1 + 1


def test_dr4_cdc_subject_never_eligible(trio: None) -> None:
    """CDC subjects are never drift targets (§10 REG-U006 / R-CDC-6) even if in the map."""
    subject = Subject.objects.create(
        subject="ecommerce.cdc.users", scenario_id=uuid4(), workspace_id=None
    )
    SchemaVersion.objects.create(
        subject=subject, workspace_id=None, version=1, json_schema=_doc(1), fingerprint="cdc1"
    )
    SchemaVersion.objects.create(
        subject=subject,
        workspace_id=None,
        version=2,
        json_schema=_doc(2, extra={"new_col": {"type": "string"}}),
        fingerprint="cdc2",
    )
    menu = build_drift_menu(
        effective={_SUBJECT: 1, "ecommerce.cdc.users": 1}, workspace_id=None
    )
    assert set(menu) == {_SUBJECT}  # the CDC subject is filtered out


def test_dr3_arming_eligible_true_when_next_version_exists(trio: None) -> None:
    """CH-V07 (DR-3): eligibility is True iff at least one subject has a registered next version."""
    assert drift_arming_eligible(effective={_SUBJECT: 1}, workspace_id=None) is True
    assert drift_arming_eligible(effective={_SUBJECT: 2}, workspace_id=None) is True
    # An empty effective map (a never-materialized / never-started stream) is ineligible.
    assert drift_arming_eligible(effective={}, workspace_id=None) is False


# -- CH-V07 config-time validation (chaos.application.validation.validate_drift_arming) --


def test_chv07_arming_drift_succeeds_when_next_version_registered(trio: None) -> None:
    """CH-V07: enabling schema_drift on a v1-effective stream with v2 registered is allowed."""
    from chaos.application.validation import validate_drift_arming

    # Effective at v1 with v2 registered ⇒ eligible: no exception.
    validate_drift_arming(
        resulting_config={"schema_drift": {"enabled": True, "rate": 0.2, "params": {}}},
        effective={_SUBJECT: 1},
        workspace_id=None,
    )


def test_chv07_arming_drift_rejected_when_no_next_version(trio: None) -> None:
    """CH-V07: enabling schema_drift at the highest version → ChaosPolicyInvalid CH-V07."""
    from chaos.application.validation import ChaosPolicyInvalid, validate_drift_arming

    with pytest.raises(ChaosPolicyInvalid) as exc:
        validate_drift_arming(
            resulting_config={"schema_drift": {"enabled": True, "rate": 0.2, "params": {}}},
            effective={_SUBJECT: 3},  # already at the highest registered version
            workspace_id=None,
        )
    codes = [e["code"] for e in exc.value.errors]
    assert codes == ["CH-V07"]
    assert exc.value.errors[0]["path"] == "/schema_drift/enabled"


def test_chv07_noop_when_drift_disabled_or_absent(trio: None) -> None:
    """CH-V07 never fires when the resulting config does not enable schema_drift."""
    from chaos.application.validation import validate_drift_arming

    # Drift disabled, even on an ineligible stream → no check.
    validate_drift_arming(
        resulting_config={"schema_drift": {"enabled": False}},
        effective={_SUBJECT: 3},
        workspace_id=None,
    )
    # Drift key absent entirely → no check (e.g. a PATCH touching only `missing`).
    validate_drift_arming(
        resulting_config={"missing": {"enabled": True, "rate": 0.1, "params": {}}},
        effective={_SUBJECT: 3},
        workspace_id=None,
    )
