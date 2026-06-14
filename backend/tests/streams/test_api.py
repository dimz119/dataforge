"""Stream API surface tests (api-spec §4.8 #39-44; W-1/W-2/W-3 masking).

Retrieve, list, the flat-collection workspace_id rule, and foreign-workspace
masking (a JWT caller who is not a member of the named/owning workspace → 404).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


@pytest.mark.django_db
def test_retrieve_stream(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _create(client, stream_ws)
    resp = client.get(f"/api/v1/streams/{sid}")
    assert resp.status_code == 200, resp.content
    assert resp.json()["stream_id"] == sid


@pytest.mark.django_db
def test_list_streams_requires_workspace_id_for_jwt(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    _create(client, stream_ws)
    # No workspace_id query param on a JWT collection route → 404 (W-2).
    assert client.get("/api/v1/streams").status_code == 404
    resp = client.get(f"/api/v1/streams?workspace_id={stream_ws.workspace.id}")
    assert resp.status_code == 200, resp.content
    assert len(resp.json()["data"]) == 1


@pytest.mark.django_db
def test_list_filter_by_status(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    url = f"/api/v1/streams?workspace_id={stream_ws.workspace.id}&status=starting"
    assert len(client.get(url).json()["data"]) == 1
    url_stopped = f"/api/v1/streams?workspace_id={stream_ws.workspace.id}&status=stopped"
    assert client.get(url_stopped).json()["data"] == []


@pytest.mark.django_db
def test_foreign_jwt_retrieve_is_404(
    client: APIClient, stream_ws: StreamWorkspaceFixture, db: Any
) -> None:
    sid = _create(client, stream_ws)
    # A different verified user not in the stream's workspace.
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair

    other = User.objects.create_user(email="outsider@example.com", password="pw-correct-horse")
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    foreign = APIClient()
    foreign.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(other).access_token}")
    assert foreign.get(f"/api/v1/streams/{sid}").status_code == 404  # W-3 masking


@pytest.mark.django_db
def test_unknown_stream_is_404(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    assert client.get(f"/api/v1/streams/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/v1/streams/not-a-uuid").status_code == 404
