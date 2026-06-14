"""Stream lifecycle verb tests (api-spec §4.8.1 #43-44; INV-STR-3).

start/stop write the DESIRED run-state + audit and are idempotent: re-issuing the
current desired state is a no-op returning current state. start is guarded to
created/stopped/failed (T2); stop overrides in-flight state (T9).
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from streams.domain.models import (
    LC_PAUSING,
    LC_STARTING,
    RUN_RUNNING,
    RUN_STOPPED,
    Stream,
)
from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture, **kwargs: object) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws, **kwargs), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


@pytest.mark.django_db
def test_start_sets_desired_running_and_starting(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _create(client, stream_ws)
    resp = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["desired_state"]["run_state"] == RUN_RUNNING
    assert body["status"] == LC_STARTING
    stream = Stream.all_objects.get(id=sid)
    assert stream.first_started_at is not None  # pin lock engaged (INV-STR-5)


@pytest.mark.django_db
def test_start_is_idempotent(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _create(client, stream_ws)
    first = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert first.status_code == 200
    first_transition = Stream.all_objects.get(id=sid).last_transition_at
    # Re-issue start on an already-running-desired stream → 200, no-op (INV-STR-3).
    second = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert second.status_code == 200
    assert second.json()["desired_state"]["run_state"] == RUN_RUNNING
    assert Stream.all_objects.get(id=sid).last_transition_at == first_transition


@pytest.mark.django_db
def test_stop_on_created_is_noop(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # A never-started stream already has desired = stopped (it is not emitting), so
    # stop is an idempotent no-op returning current state (INV-STR-3).
    sid = _create(client, stream_ws)  # never started; desired stopped, lifecycle created
    resp = client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["desired_state"]["run_state"] == RUN_STOPPED
    assert body["status"] == "created"  # no desired change → no-op


@pytest.mark.django_db
def test_stop_from_started_then_restop_to_stopped(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # A started stream that is stopped goes to lifecycle 'stopping'; the runner
    # finalizes to 'stopped' (T10). Here we assert the desired write + stopping nudge.
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    resp = client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopping"


@pytest.mark.django_db
def test_stop_is_idempotent(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _create(client, stream_ws)
    first = client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert first.status_code == 200
    first_transition = Stream.all_objects.get(id=sid).last_transition_at
    second = client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert second.status_code == 200
    assert second.json()["desired_state"]["run_state"] == RUN_STOPPED
    assert Stream.all_objects.get(id=sid).last_transition_at == first_transition


@pytest.mark.django_db
def test_stop_overrides_running(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    resp = client.post(f"/api/v1/streams/{sid}/stop", format="json")
    assert resp.status_code == 200
    # A started stream nudges to stopping (the runner finalizes, T10).
    assert resp.json()["desired_state"]["run_state"] == RUN_STOPPED
    assert resp.json()["status"] == "stopping"


@pytest.mark.django_db
def test_start_from_pausing_is_409(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _create(client, stream_ws)
    # Force a non-startable lifecycle state (pausing) via the model.
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_PAUSING)
    resp = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert resp.status_code == 409, resp.content


@pytest.mark.django_db
def test_start_then_stop_audits(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    from audit.domain.models import AuditLog

    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")
    client.post(f"/api/v1/streams/{sid}/stop", format="json")
    actions = set(
        AuditLog.objects.filter(target_id=sid).values_list("action", flat=True)
    )
    assert "streams.stream.start_requested" in actions
    assert "streams.stream.stop_requested" in actions
