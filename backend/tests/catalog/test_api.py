"""Catalog read/write API (api-spec §4.6 #26-32, §4.7 #33-38).

Exit criterion #3: GET /scenarios works; responses validate against the OpenAPI
artifact; RFC 9457 errors. These exercise the wire shapes + the auth/visibility
rules; the cross-tenant masking is the permanent TEN suite's job.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.catalog.conftest import AuthedWorkspace
from tests.catalog.fixtures import valid_subset_manifest

pytestmark = pytest.mark.django_db


def test_list_scenarios_returns_globals(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/scenarios")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    slugs = {row["scenario_slug"] for row in body["data"]}
    assert "ecommerce" in slugs
    eco = next(r for r in body["data"] if r["scenario_slug"] == "ecommerce")
    assert eco["visibility"] == "global"
    assert eco["latest_version"] == "1.0.0"
    assert eco["published_versions"] == ["1.0.0"]


def test_list_scenarios_requires_auth() -> None:
    from rest_framework.test import APIClient

    resp = APIClient().get("/api/v1/scenarios")
    assert resp.status_code == 401
    assert resp["Content-Type"] == "application/problem+json"


def test_scenario_detail_includes_versions(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/scenarios/ecommerce")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scenario_slug"] == "ecommerce"
    assert any(v["manifest_version"] == "1.0.0" and v["status"] == "published"
               for v in body["versions"])


def test_scenario_version_detail_carries_document(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    resp = authed_workspace.client.get("/api/v1/scenarios/ecommerce/versions/1.0.0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["document"]["metadata"]["slug"] == "ecommerce"
    assert len(body["sha256"]) == 64


def test_unknown_scenario_is_404_problem(authed_workspace: AuthedWorkspace) -> None:
    resp = authed_workspace.client.get("/api/v1/scenarios/nope")
    assert resp.status_code == 404
    assert resp.json()["type"].endswith("/not-found")


def test_create_draft_then_validation_poll(authed_workspace: AuthedWorkspace) -> None:
    manifest = valid_subset_manifest()
    manifest["metadata"]["slug"] = "wsdraft"
    resp = authed_workspace.client.post(
        "/api/v1/scenarios",
        data={"workspace_id": str(authed_workspace.workspace.id), "document": manifest},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json()["status"] == "draft"
    # The validation poll target (#30) returns the persisted passed report.
    poll = authed_workspace.client.get(
        "/api/v1/scenarios/wsdraft/versions/1.0.0/validation"
        f"?workspace_id={authed_workspace.workspace.id}"
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == "passed"


def test_create_draft_invalid_manifest_is_422(authed_workspace: AuthedWorkspace) -> None:
    bad = valid_subset_manifest()
    bad["metadata"]["slug"] = "badness"
    del bad["entities"]  # structural failure
    resp = authed_workspace.client.post(
        "/api/v1/scenarios",
        data={"workspace_id": str(authed_workspace.workspace.id), "document": bad},
        format="json",
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("/manifest-validation-failed")
    assert body["errors"], "must carry MAN-* errors"


def test_publish_workspace_version_derives_schemas(
    authed_workspace: AuthedWorkspace,
) -> None:
    manifest = valid_subset_manifest()
    manifest["metadata"]["slug"] = "pubme"
    authed_workspace.client.post(
        "/api/v1/scenarios",
        data={"workspace_id": str(authed_workspace.workspace.id), "document": manifest},
        format="json",
    )
    resp = authed_workspace.client.post(
        "/api/v1/scenarios/pubme/versions/1.0.0/publish"
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == "published"
    from registry.domain.models import Subject

    assert Subject.objects.filter(workspace_id=authed_workspace.workspace.id).exists()


def test_scenario_instance_lifecycle(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    ws = authed_workspace.workspace.id
    create = authed_workspace.client.post(
        f"/api/v1/workspaces/{ws}/scenario-instances",
        data={"name": "lab-1", "scenario_slug": "ecommerce", "manifest_version": "1.0.0"},
        format="json",
    )
    assert create.status_code == 201, create.content
    instance_id = create.json()["scenario_instance_id"]
    assert create.json()["config_revision"] == 1

    # GET configuration.
    cfg = authed_workspace.client.get(
        f"/api/v1/workspaces/{ws}/scenario-instances/{instance_id}/configuration"
    )
    assert cfg.status_code == 200
    assert cfg.json()["config_revision"] == 1

    # PUT a valid overlay → revision bumps.
    put = authed_workspace.client.put(
        f"/api/v1/workspaces/{ws}/scenario-instances/{instance_id}/configuration",
        data={"configuration": {"catalog_sizes": {"users": 5000}}},
        format="json",
    )
    assert put.status_code == 200, put.content
    assert put.json()["config_revision"] == 2

    # DELETE.
    delete = authed_workspace.client.delete(
        f"/api/v1/workspaces/{ws}/scenario-instances/{instance_id}"
    )
    assert delete.status_code == 204


def test_instance_invalid_overlay_is_422_with_override_scope(
    published_ecommerce: Any, authed_workspace: AuthedWorkspace
) -> None:
    ws = authed_workspace.workspace.id
    # An out-of-bounds catalog size trips a Layer-2 bound (override scope).
    resp = authed_workspace.client.post(
        f"/api/v1/workspaces/{ws}/scenario-instances",
        data={
            "name": "bad-overlay",
            "scenario_slug": "ecommerce",
            "manifest_version": "1.0.0",
            "configuration": {"catalog_sizes": {"users": 10_000_000}},
        },
        format="json",
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("/manifest-validation-failed")
    assert all(e.get("scope") == "override" for e in body["errors"])


def test_unknown_workspace_instance_list_is_404(authed_workspace: AuthedWorkspace) -> None:
    resp = authed_workspace.client.get(
        f"/api/v1/workspaces/{uuid.uuid4()}/scenario-instances"
    )
    assert resp.status_code == 404
