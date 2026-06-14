"""Stream quota-cap tests (INV-TEN-5; api-spec §4.8).

The two synchronously-checkable caps: per-stream TPS (at create) and concurrent
streams (at start). Both are read from the workspace's Free-tier quota row.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from tenancy.domain.models import WorkspaceQuotas
from tests.streams.conftest import StreamWorkspaceFixture, create_body


@pytest.mark.django_db
def test_create_above_per_stream_tps_cap_is_403(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # Free per-stream cap is 50; request 1000 (within the v1 request bound but over cap).
    resp = client.post("/api/v1/streams", create_body(stream_ws, target_tps=1000), format="json")
    assert resp.status_code == 403, resp.content
    body = resp.json()
    assert body["quota"] == "per_stream_tps"
    assert body["limit"] == 50


@pytest.mark.django_db
def test_start_above_concurrent_cap_is_403(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # Free concurrent cap is 2. Create + start two streams (under cap), then a third.
    ids = []
    for i in range(3):
        resp = client.post(
            "/api/v1/streams",
            create_body(stream_ws, name=f"s{i}", target_tps=10),
            format="json",
        )
        assert resp.status_code == 201, resp.content
        ids.append(str(resp.json()["stream_id"]))
    assert client.post(f"/api/v1/streams/{ids[0]}/start", format="json").status_code == 200
    assert client.post(f"/api/v1/streams/{ids[1]}/start", format="json").status_code == 200
    # The third start exceeds the concurrent cap of 2.
    third = client.post(f"/api/v1/streams/{ids[2]}/start", format="json")
    assert third.status_code == 403, third.content
    assert third.json()["quota"] == "concurrent_streams"
    assert third.json()["limit"] == 2


@pytest.mark.django_db
def test_higher_plan_cap_permits_more(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # Bump the workspace plan caps; the per-stream cap then permits a higher TPS.
    WorkspaceQuotas.all_objects.filter(workspace_id=stream_ws.workspace.id).update(
        per_stream_tps_cap=1000
    )
    resp = client.post("/api/v1/streams", create_body(stream_ws, target_tps=1000), format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["desired_state"]["target_tps"] == 1000
