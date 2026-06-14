"""Registry read API (api-spec §4.12 #62-65; schema-registry §7).

Exit criterion #3: GET /schemas/{subject}/versions works; RFC 9457 errors. Reads
are JWT|Key(schemas:read); global subjects readable by any authenticated principal.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.registry.conftest import AuthedWorkspace

pytestmark = pytest.mark.django_db


def test_list_schemas_returns_global_subjects(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    subjects = {row["subject"] for row in body["data"]}
    assert "ecommerce.order_placed" in subjects
    assert "ecommerce.cdc.users" in subjects
    op = next(r for r in body["data"] if r["subject"] == "ecommerce.order_placed")
    assert op["scenario_slug"] == "ecommerce"
    assert op["compatibility"] == "BACKWARD_ADDITIVE"
    assert op["latest_version"] == 1
    assert op["versions"] == [1]


def test_list_schemas_filtered_by_scenario(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas?scenario_slug=ecommerce")
    assert resp.status_code == 200
    assert all(r["scenario_slug"] == "ecommerce" for r in resp.json()["data"])


def test_list_schemas_requires_auth() -> None:
    from rest_framework.test import APIClient

    resp = APIClient().get("/api/v1/schemas")
    assert resp.status_code == 401
    assert resp["Content-Type"] == "application/problem+json"


def test_subject_detail_has_provenance(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas/ecommerce.order_placed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "ecommerce.order_placed"
    assert "created_at" in body
    prov = body["version_provenance"]
    assert prov[0]["version"] == 1
    assert prov[0]["manifest_version"] == "1.0.0"  # Flow-1 provenance


def test_versions_list(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas/ecommerce.order_placed/versions")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == [{"version": 1, "registered_at": data[0]["registered_at"],
                     "manifest_version": "1.0.0"}]


def test_version_detail_carries_schema_document(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get(
        "/api/v1/schemas/ecommerce.order_placed/versions/1"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["manifest_version"] == "1.0.0"
    schema = body["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["currency"] == {"const": "USD"}


def test_version_latest_alias(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get(
        "/api/v1/schemas/ecommerce.order_placed/versions/latest"
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1


def test_unknown_subject_is_404(authed_workspace: AuthedWorkspace) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas/ecommerce.nope")
    assert resp.status_code == 404
    assert resp.json()["type"].endswith("/not-found")


def test_cdc_subject_routes_with_dots(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/schemas/ecommerce.cdc.users/versions/1")
    assert resp.status_code == 200
    assert resp.json()["subject"] == "ecommerce.cdc.users"
