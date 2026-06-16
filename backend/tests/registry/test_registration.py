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


def test_flow1_added_payload_field_registers_optional_v2(db: Any) -> None:
    """A manifest re-publish adding a payload field registers v2 under REQ-RULE.

    The new field enters ``properties`` but NOT ``required`` (§4.1 REQ-RULE): the
    required set is carried forward from v1 exactly, so the §6 ``required``-set check
    (REG-C003) passes and the addition is accepted as an additive minor bump. The
    added field carries an ``x-df-binding`` copied from its manifest valueSource
    (§5.3), which is stripped from comparison form so it never affects the gate.
    """
    manifest = _manifest_with_one_event()
    _register(manifest)
    v1 = SchemaVersion.objects.get(subject__subject="scn.thing", version=1)
    assert set(v1.json_schema["required"]) == {"user_id"}

    evolved = copy.deepcopy(manifest)
    evolved["event_types"]["thing"]["payload"]["name"] = {"from": "actor.name"}
    result = _register(evolved)

    thing = next(r for r in result if r.subject == "scn.thing")
    assert thing.created
    assert thing.version == 2
    v2 = SchemaVersion.objects.get(subject__subject="scn.thing", version=2)
    # REQ-RULE: required unchanged from v1; the new field is optional.
    assert set(v2.json_schema["required"]) == set(v1.json_schema["required"]) == {"user_id"}
    assert "name" in v2.json_schema["properties"]
    assert "name" not in v2.json_schema["required"]
    # §5.3: the new optional field carries its manifest binding verbatim.
    assert v2.json_schema["properties"]["name"]["x-df-binding"] == {"from": "actor.name"}
    assert SchemaVersion.objects.filter(subject__subject="scn.thing").count() == 2


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
