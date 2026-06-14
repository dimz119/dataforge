"""Generation dataset API tests (api-spec §4.10 routes #57-61, SQLite unit lane).

Exercises the HTTP surface end to end with a JWT-authenticated client: create
(sync 201 → ready), status poll, download (delivered-shape gzip), quota 403,
non-ready 409, and delete 204. RLS masking is proven in the Postgres lane + the
permanent TEN cross-tenant suite (the new routes are classified in access_policy).
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest
from rest_framework.test import APIClient

from dataforge_engine.envelope import DELIVERED_FIELD_SET
from tests.generation.conftest import WorkspaceFixture


@pytest.fixture
def client(gen_workspace: WorkspaceFixture) -> APIClient:
    from identity.infra.jwt import issue_token_pair

    token = issue_token_pair(gen_workspace.admin)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return api


def _create_body(gen_workspace: WorkspaceFixture, **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workspace_id": str(gen_workspace.workspace.id),
        "scenario_instance_id": str(gen_workspace.instance.id),
        "name": "june-backfill",
        "seed": "42",
        "simulated_days": 1,
        "compression": "gzip",
    }
    body.update(kwargs)
    return body


@pytest.mark.django_db
def test_create_sync_returns_201_ready(
    client: APIClient, gen_workspace: WorkspaceFixture
) -> None:
    resp = client.post("/api/v1/datasets", _create_body(gen_workspace), format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["status"] == "ready"
    assert body["event_count"] > 0
    assert body["seed"] == "42"
    assert resp["Location"] == f"/api/v1/datasets/{body['dataset_id']}"


@pytest.mark.django_db
def test_status_poll_and_list(client: APIClient, gen_workspace: WorkspaceFixture) -> None:
    created = client.post(
        "/api/v1/datasets", _create_body(gen_workspace), format="json"
    ).json()
    ws = gen_workspace.workspace.id
    detail = client.get(f"/api/v1/datasets/{created['dataset_id']}?workspace_id={ws}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "ready"
    listed = client.get(f"/api/v1/datasets?workspace_id={ws}")
    assert listed.status_code == 200
    assert any(d["dataset_id"] == created["dataset_id"] for d in listed.json()["data"])


@pytest.mark.django_db
def test_download_streams_delivered_shape(
    client: APIClient, gen_workspace: WorkspaceFixture
) -> None:
    created = client.post(
        "/api/v1/datasets", _create_body(gen_workspace), format="json"
    ).json()
    ws = gen_workspace.workspace.id
    resp = client.get(
        f"/api/v1/datasets/{created['dataset_id']}/download?workspace_id={ws}"
    )
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/gzip"
    assert "attachment" in resp["Content-Disposition"]
    raw = b"".join(getattr(resp, "streaming_content", []))
    lines = [ln for ln in gzip.decompress(raw).decode("utf-8").splitlines() if ln]
    assert lines
    obj = json.loads(lines[0])
    assert "_df" not in obj
    assert set(obj.keys()) == DELIVERED_FIELD_SET


@pytest.mark.django_db
def test_quota_cap_returns_403(client: APIClient, gen_workspace: WorkspaceFixture) -> None:
    resp = client.post(
        "/api/v1/datasets",
        _create_body(gen_workspace, simulated_days=30, name="too-big"),
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["type"].endswith("quota-exceeded")


@pytest.mark.django_db
def test_download_non_ready_returns_409(
    client: APIClient, gen_workspace: WorkspaceFixture
) -> None:
    from generation.domain.models import DATASET_GENERATING, Dataset

    created = client.post(
        "/api/v1/datasets", _create_body(gen_workspace), format="json"
    ).json()
    ds = Dataset.all_objects.get(id=created["dataset_id"])
    ds.status = DATASET_GENERATING
    ds.save(update_fields=["status"])
    ws = gen_workspace.workspace.id
    resp = client.get(f"/api/v1/datasets/{created['dataset_id']}/download?workspace_id={ws}")
    assert resp.status_code == 409
    assert resp.json()["type"].endswith("invalid-state-transition")


@pytest.mark.django_db
def test_delete_removes_dataset(client: APIClient, gen_workspace: WorkspaceFixture) -> None:
    from generation.domain.models import Dataset

    created = client.post(
        "/api/v1/datasets", _create_body(gen_workspace), format="json"
    ).json()
    ws = gen_workspace.workspace.id
    resp = client.delete(f"/api/v1/datasets/{created['dataset_id']}?workspace_id={ws}")
    assert resp.status_code == 204
    assert not Dataset.all_objects.filter(id=created["dataset_id"]).exists()


@pytest.mark.django_db
def test_foreign_dataset_id_masks_to_404(
    client: APIClient, gen_workspace: WorkspaceFixture
) -> None:
    import uuid

    ws = gen_workspace.workspace.id
    resp = client.get(f"/api/v1/datasets/{uuid.uuid4()}?workspace_id={ws}")
    assert resp.status_code == 404
