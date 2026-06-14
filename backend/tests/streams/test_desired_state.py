"""Desired-state read service tests (backend-architecture §8.3; ADR-0006).

The runner-facing read: one batched call returns every claimable shard's desired
state as immutable value objects carrying the pin. A stopped/created stream is NOT
claimable; a running-desired or converging stream IS.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from streams.application import desired_state
from streams.domain.models import Stream
from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture, **kwargs: object) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws, **kwargs), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


@pytest.mark.django_db
def test_created_stream_not_claimable(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    _create(client, stream_ws)  # desired stopped, lifecycle created
    assert desired_state.claimable_desired_states() == []


@pytest.mark.django_db
def test_started_stream_is_claimable_with_pin(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    rows = desired_state.claimable_desired_states()
    assert len(rows) == 1
    row = rows[0]
    assert str(row.stream_id) == sid
    assert row.run_state == "running"
    assert row.target_tps == 50
    assert row.seed == 424242424242  # the immutable pin (INV-STR-5)
    assert row.scenario_slug == "ecommerce"
    assert row.manifest_version == "1.0.0"
    assert row.pinned_config  # merged config snapshot
    assert row.workspace_id == stream_ws.workspace.id  # for per-shard workspace_scope


@pytest.mark.django_db
def test_desired_for_one_stream(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    row = desired_state.desired_for(sid)
    assert row is not None
    assert str(row.stream_id) == sid
    assert not row.is_stopped


@pytest.mark.django_db
def test_stopping_stream_still_surfaced_for_finalize(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # A started-then-stopped stream is in lifecycle 'stopping' (desired stopped) — it
    # must still be surfaced so the runner reaches finalize (T10).
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert Stream.all_objects.get(id=sid).lifecycle_state == "stopping"
    ids = {str(r.stream_id) for r in desired_state.claimable_desired_states()}
    assert sid in ids


@pytest.mark.django_db
def test_desired_for_unknown_is_none(db: object) -> None:
    import uuid

    assert desired_state.desired_for(uuid.uuid4()) is None
