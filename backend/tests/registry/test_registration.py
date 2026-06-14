"""Registry registration invariants (schema-registry §5.1; INV-REG-1/2/3).

Subjects created only by publication; versions monotonic from 1, immutable;
identical re-derivation registers nothing (R-DER-4 fingerprint no-op);
non-additive candidates raise SchemaCompatibilityError.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

import pytest

from registry.application.registration import (
    SchemaCompatibilityError,
    register_derived_schemas,
)
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_SCENARIO = uuid4()
_DEF = uuid4()


def _manifest_with_one_event() -> dict[str, Any]:
    """A tiny manifest deriving exactly one business subject (``scn.thing``)."""
    return {
        "manifest_schema": "v0",
        "metadata": {"slug": "scn", "version": "1.0.0", "actor_entity": "users"},
        "entities": {
            "users": {
                "key_prefix": "usr",
                "key_attribute": "user_id",
                "attributes": {"name": {"generator": "person.full_name"}},
            }
        },
        "event_types": {
            "thing": {"payload": {"user_id": {"from": "actor.user_id"}}}
        },
        "state_machines": {
            "m": {
                "type": "session",
                "states": {
                    "s": {"transitions": [{"to": "s", "emit": "thing", "probability": 0.5}]}
                },
            }
        },
    }


def _register(manifest: dict[str, Any]) -> Any:
    return register_derived_schemas(
        manifest, scenario_id=_SCENARIO, workspace_id=None, definition_id=_DEF
    )


def test_first_publication_creates_subject_and_v1(published_ecommerce: Any) -> None:
    subjects = Subject.objects.all()
    assert subjects.count() == 13
    op = Subject.objects.get(subject="ecommerce.order_placed")
    assert op.workspace_id is None  # global subject (INV-REG-4)
    assert op.compatibility_mode == "BACKWARD_ADDITIVE"
    versions = list(SchemaVersion.objects.filter(subject=op))
    assert len(versions) == 1
    assert versions[0].version == 1


def test_subject_names_match_inv_reg_1(published_ecommerce: Any) -> None:
    names = {s.subject for s in Subject.objects.all()}
    # Business: {slug}.{event}; CDC: {slug}.cdc.{entity}.
    assert "ecommerce.order_placed" in names
    assert "ecommerce.cdc.orders" in names
    assert all(n.startswith("ecommerce.") for n in names)


def test_identical_rederivation_registers_nothing(db: Any) -> None:
    manifest = _manifest_with_one_event()
    first = _register(manifest)
    assert all(r.created for r in first)
    assert SchemaVersion.objects.count() == 1
    # Re-register the identical manifest → R-DER-4 no-op.
    second = _register(copy.deepcopy(manifest))
    assert all(not r.created for r in second)
    assert second[0].version == 1
    assert SchemaVersion.objects.count() == 1


def test_flow1_added_payload_field_trips_c003(db: Any) -> None:
    """A manifest re-publish adding a payload field derives an all-required schema.

    Because R-DER-3 makes every derived field ``required``, a new field changes the
    required set → REG-C003. Manifest minor-version evolution (a field added with a
    binding, NOT required) is the Flow-2 explicit-evolution command (Phase 10); a
    Phase-3 re-publish is therefore byte-identical (no-op) or rejected. This pins
    the §6.4 "the live Flow 1 failures are C001/C002/C003" contract.
    """
    manifest = _manifest_with_one_event()
    _register(manifest)
    evolved = copy.deepcopy(manifest)
    evolved["event_types"]["thing"]["payload"]["name"] = {"from": "actor.name"}
    with pytest.raises(SchemaCompatibilityError) as exc:
        _register(evolved)
    assert any(e.code == "REG-C003" for e in exc.value.errors)
    assert SchemaVersion.objects.filter(subject__subject="scn.thing").count() == 1


def test_non_additive_change_raises_compat_error(db: Any) -> None:
    manifest = _manifest_with_one_event()
    _register(manifest)
    # Retype the existing user_id field → REG-C002 (existing field frozen).
    broken = copy.deepcopy(manifest)
    broken["entities"]["users"]["key_attribute"] = "user_id"
    broken["event_types"]["thing"]["payload"]["user_id"] = {"const": "x"}
    with pytest.raises(SchemaCompatibilityError) as exc:
        _register(broken)
    assert exc.value.subject == "scn.thing"
    assert any(e.code == "REG-C002" for e in exc.value.errors)
    # No v2 was written (the publish txn would roll back).
    assert SchemaVersion.objects.filter(subject__subject="scn.thing").count() == 1
