"""Schema diff API — additive-only diffs over the v1/v2/v3 trio (Exit #6 API side).

``GET /api/v1/schemas/{subject}/diff?from=a&to=b`` (api-spec §4.12 #66): the computed
``added(to) \\ added(from)`` diff (§5.3), each ``{path, type, required:false}``;
``removed``/``changed`` always ``[]`` under BACKWARD_ADDITIVE (INV-REG-3). This pins
the v1→v2 (``shipping_state``), v2→v3 (``shipping_city``) and the 1→3 union, plus the
two error paths: ``from ≥ to`` → 400 ``validation-error``; an absent version → 404.

The diff is computed, never stored (§3.2). Auth is ``schemas:read``. Runs under the
maintenance role (the v2/v3 globals are seeded through Flow 2).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.management import call_command

from tests.registry.conftest import AuthedWorkspace

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"


@pytest.fixture
def seeded(published_ecommerce: Any) -> Any:
    """Publish ecommerce then seed the v2/v3 evolutions (the diff corpus)."""
    call_command("seed_schema_evolutions", stdout=io.StringIO())
    return published_ecommerce


def _diff(client: Any, frm: int, to: int) -> Any:
    return client.get(f"/api/v1/schemas/{_SUBJECT}/diff?from={frm}&to={to}")


def test_diff_v1_to_v2_adds_only_shipping_state(
    seeded: Any, authed_workspace: AuthedWorkspace
) -> None:
    """v1→v2: a single green addition ``shipping_state`` (optional); no removals/changes."""
    resp = _diff(authed_workspace.client, 1, 2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == _SUBJECT
    assert (body["from_version"], body["to_version"]) == (1, 2)
    assert body["added_fields"] == [
        {"path": "/properties/shipping_state", "type": "string", "required": False}
    ]
    assert body["removed_fields"] == []
    assert body["changed_fields"] == []


def test_diff_v2_to_v3_adds_only_shipping_city(
    seeded: Any, authed_workspace: AuthedWorkspace
) -> None:
    """v2→v3: a single green addition ``shipping_city``."""
    resp = _diff(authed_workspace.client, 2, 3)
    assert resp.status_code == 200
    added = {f["path"] for f in resp.json()["added_fields"]}
    assert added == {"/properties/shipping_city"}


def test_diff_v1_to_v3_is_the_union_in_introduction_order(
    seeded: Any, authed_workspace: AuthedWorkspace
) -> None:
    """1→3 is the per-step concatenation (shipping_state then shipping_city, §7)."""
    resp = _diff(authed_workspace.client, 1, 3)
    assert resp.status_code == 200
    paths = [f["path"] for f in resp.json()["added_fields"]]
    assert paths == ["/properties/shipping_state", "/properties/shipping_city"]


def test_diff_from_ge_to_is_400(seeded: Any, authed_workspace: AuthedWorkspace) -> None:
    """``from ≥ to`` is a validation error (a diff is forward-only)."""
    equal = _diff(authed_workspace.client, 2, 2)
    reversed_ = _diff(authed_workspace.client, 3, 1)
    assert equal.status_code == 400
    assert reversed_.status_code == 400
    assert equal["Content-Type"] == "application/problem+json"


def test_diff_absent_version_is_404(
    seeded: Any, authed_workspace: AuthedWorkspace
) -> None:
    """A version above the latest registered is absent → 404."""
    resp = _diff(authed_workspace.client, 1, 99)
    assert resp.status_code == 404
    assert resp.json()["type"].endswith("/not-found")


def test_diff_requires_auth(seeded: Any) -> None:
    """Unauthenticated diff is 401 with a problem+json body (A-4)."""
    from rest_framework.test import APIClient

    resp = APIClient().get(f"/api/v1/schemas/{_SUBJECT}/diff?from=1&to=2")
    assert resp.status_code == 401
    assert resp["Content-Type"] == "application/problem+json"
