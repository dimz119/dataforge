"""v2/v3 evolutions register correctly via the seed command (Exit #1, #4).

Phase-10 scope item 1 + exit criterion #1 ("consumers resolve both versions from
the registry"): ``manage.py seed_schema_evolutions`` registers the curated builtin
``ecommerce.order_placed`` v2 (adds optional ``shipping_state``) then v3 (adds
optional ``shipping_city``) through Flow 2 against the published builtin scenario.

This pins: the two versions register at v2/v3 with the documented shipping additions
and the required set carried forward unchanged (REQ-RULE); the command is idempotent
+ re-runnable (running it twice leaves the subject at v3); and the per-version read
API serves both documents verbatim (the consumer-side resolution exercise E5).

Runs under the maintenance role (it INSERTs global ``schema_versions`` rows, §5.2).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.management import call_command

from registry.domain.models import SchemaVersion
from tests.registry.conftest import AuthedWorkspace

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"


def _seed() -> None:
    """Run the real seed command (its default --fixture-dir = the repo fixtures)."""
    call_command("seed_schema_evolutions", stdout=io.StringIO())


def test_seed_registers_v2_and_v3(published_ecommerce: Any) -> None:
    """After the seed the subject is at v3 with exactly versions [1, 2, 3]."""
    _seed()
    versions = list(
        SchemaVersion.objects.filter(subject__subject=_SUBJECT).order_by("version")
    )
    assert [v.version for v in versions] == [1, 2, 3]


def test_v2_adds_optional_shipping_state(published_ecommerce: Any) -> None:
    """v2 adds ``shipping_state`` to properties (optional) — required set unchanged."""
    _seed()
    v1 = SchemaVersion.objects.get(subject__subject=_SUBJECT, version=1)
    v2 = SchemaVersion.objects.get(subject__subject=_SUBJECT, version=2)
    assert "shipping_state" in v2.json_schema["properties"]
    assert "shipping_state" not in v1.json_schema["properties"]
    # REQ-RULE: required carried forward verbatim, the added field optional.
    assert set(v2.json_schema["required"]) == set(v1.json_schema["required"])
    assert "shipping_state" not in v2.json_schema["required"]
    # §5.3: the added field carries its binding verbatim (served, never gated on).
    assert v2.json_schema["properties"]["shipping_state"]["x-df-binding"] == {
        "from": "actor.address.state"
    }


def test_v3_adds_optional_shipping_city_on_top_of_v2(published_ecommerce: Any) -> None:
    """v3 adds ``shipping_city`` (and keeps ``shipping_state`` from v2)."""
    _seed()
    v3 = SchemaVersion.objects.get(subject__subject=_SUBJECT, version=3)
    assert "shipping_city" in v3.json_schema["properties"]
    assert "shipping_state" in v3.json_schema["properties"]  # carried from v2
    assert "shipping_city" not in v3.json_schema["required"]
    assert v3.json_schema["properties"]["shipping_city"]["x-df-binding"] == {
        "from": "actor.address.city"
    }


def test_seed_is_idempotent(published_ecommerce: Any) -> None:
    """Re-running the seed leaves the subject at v3 (no fourth version, no errors)."""
    _seed()
    _seed()  # the re-run must be a no-op for both v2 and v3
    assert SchemaVersion.objects.filter(subject__subject=_SUBJECT).count() == 3


def test_consumer_resolves_both_versions_from_read_api(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    """Exit #1: a consumer fetches v1 and v2 documents from the registry read API."""
    _seed()
    client = authed_workspace.client
    v1 = client.get(f"/api/v1/schemas/{_SUBJECT}/versions/1")
    v2 = client.get(f"/api/v1/schemas/{_SUBJECT}/versions/2")
    assert v1.status_code == 200 and v2.status_code == 200
    assert "shipping_state" not in v1.json()["schema"]["properties"]
    assert "shipping_state" in v2.json()["schema"]["properties"]
    # ``latest`` now resolves to v3.
    latest = client.get(f"/api/v1/schemas/{_SUBJECT}/versions/latest")
    assert latest.json()["version"] == 3
