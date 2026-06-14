"""Stream pause/resume + live PATCH(target_tps) control-plane tests (Phase 6).

The control plane writes desired ``paused``/``running``/``target_tps`` + audit; the
runner converges (T6 checkpoint-on-pause; T8 restore + dwell rebase). Covered here
(the control-plane half — the runner half is tests/runner):

* pause/resume idempotency (INV-STR-3) and the T5/T7 lifecycle guards (409);
* the pause ``status_reason`` plumbing (user vs system quota/idle → paused_quota/idle);
* PATCH target_tps: write + audit, quota cap at command time (INV-TEN-5), out-of-range
  400, idempotent no-op, and the immutable-field rejection (PIN-4).
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from streams.domain.models import (
    LC_PAUSED,
    LC_PAUSING,
    LC_RESUMING,
    LC_RUNNING,
    REASON_IDLE,
    REASON_QUOTA,
    RUN_PAUSED,
    RUN_RUNNING,
    Stream,
)
from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture, **kwargs: object) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws, **kwargs), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


def _started_running(client: APIClient, stream_ws: StreamWorkspaceFixture, **kw: object) -> str:
    """Create + start, then force lifecycle ``running`` (the runner-converged state)."""
    sid = _create(client, stream_ws, **kw)
    assert client.post(f"/api/v1/streams/{sid}/start", format="json").status_code == 200
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_RUNNING)
    return sid


# --- pause (T5) -------------------------------------------------------------


@pytest.mark.django_db
def test_pause_sets_desired_paused_and_pausing(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)
    resp = client.post(f"/api/v1/streams/{sid}/pause", format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["desired_state"]["run_state"] == RUN_PAUSED
    assert body["status"] == LC_PAUSING  # nudge → pausing; runner converges to paused
    assert body["status_reason"] == "user"


@pytest.mark.django_db
def test_pause_is_idempotent(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _started_running(client, stream_ws)
    first = client.post(f"/api/v1/streams/{sid}/pause", format="json")
    assert first.status_code == 200
    first_transition = Stream.all_objects.get(id=sid).last_transition_at
    # Re-issue pause on a paused-desired stream → 200 no-op (INV-STR-3).
    second = client.post(f"/api/v1/streams/{sid}/pause", format="json")
    assert second.status_code == 200
    assert second.json()["desired_state"]["run_state"] == RUN_PAUSED
    assert Stream.all_objects.get(id=sid).last_transition_at == first_transition


@pytest.mark.django_db
def test_pause_from_created_is_409(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # A never-started stream (lifecycle created) is not pausable (T5 guard).
    sid = _create(client, stream_ws)
    resp = client.post(f"/api/v1/streams/{sid}/pause", format="json")
    assert resp.status_code == 409, resp.content


@pytest.mark.django_db
def test_pause_audits(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    from audit.domain.models import AuditLog

    sid = _started_running(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/pause", format="json")
    actions = set(AuditLog.objects.filter(target_id=sid).values_list("action", flat=True))
    assert "streams.stream.pause_requested" in actions


# --- status_reason rendering (paused_quota / paused_idle; Phase 11 triggers) -


@pytest.mark.django_db
def test_status_reason_renders_paused_quota(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # The quota/idle TRIGGERS are Phase 11; the rendering is wired now. Force a
    # quota-reason paused row and assert the surfaced status string.
    sid = _started_running(client, stream_ws)
    Stream.all_objects.filter(id=sid).update(
        lifecycle_state=LC_PAUSED, desired_state=RUN_PAUSED, status_reason=REASON_QUOTA
    )
    body = client.get(f"/api/v1/streams/{sid}").json()
    assert body["status"] == "paused_quota"
    Stream.all_objects.filter(id=sid).update(status_reason=REASON_IDLE)
    assert client.get(f"/api/v1/streams/{sid}").json()["status"] == "paused_idle"


# --- resume (T7) ------------------------------------------------------------


@pytest.mark.django_db
def test_resume_sets_desired_running_and_resuming(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/pause", format="json")
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_PAUSED)  # runner converged
    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["desired_state"]["run_state"] == RUN_RUNNING
    assert body["status"] == LC_RESUMING  # nudge → resuming; runner converges to running


@pytest.mark.django_db
def test_resume_is_idempotent(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    sid = _started_running(client, stream_ws)
    # A running-desired stream: resume is a no-op (INV-STR-3).
    first_transition = Stream.all_objects.get(id=sid).last_transition_at
    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 200
    assert resp.json()["desired_state"]["run_state"] == RUN_RUNNING
    assert Stream.all_objects.get(id=sid).last_transition_at == first_transition


@pytest.mark.django_db
def test_resume_from_running_is_noop_not_409(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # Resume on an already-running-desired stream short-circuits to the no-op BEFORE
    # the guard, so a running lifecycle does not raise 409 (INV-STR-3 precedence).
    sid = _started_running(client, stream_ws)
    assert client.post(f"/api/v1/streams/{sid}/resume", format="json").status_code == 200


@pytest.mark.django_db
def test_resume_from_stopped_is_409(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    from streams.domain.models import LC_STOPPED, RUN_STOPPED

    sid = _create(client, stream_ws)
    Stream.all_objects.filter(id=sid).update(
        lifecycle_state=LC_STOPPED, desired_state=RUN_STOPPED
    )
    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 409, resp.content


@pytest.mark.django_db
def test_resume_quota_paused_requires_headroom(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # A quota-paused stream over the per-stream cap cannot resume (T7 headroom guard,
    # INV-TEN-5): the runner-side cap is 50, the stream's target_tps is forced to 60.
    sid = _started_running(client, stream_ws)
    Stream.all_objects.filter(id=sid).update(
        lifecycle_state=LC_PAUSED,
        desired_state=RUN_PAUSED,
        status_reason=REASON_QUOTA,
        target_tps=60,
    )
    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 403, resp.content
    assert resp.json()["quota"] == "per_stream_tps"


# --- PATCH target_tps (live mutation, §4.8.2) -------------------------------


@pytest.mark.django_db
def test_patch_target_tps_writes_desired_and_audits(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    from audit.domain.models import AuditLog

    sid = _started_running(client, stream_ws)  # created target_tps=50
    resp = client.patch(f"/api/v1/streams/{sid}", {"target_tps": 40}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["desired_state"]["target_tps"] == 40
    assert Stream.all_objects.get(id=sid).target_tps == 40
    actions = set(AuditLog.objects.filter(target_id=sid).values_list("action", flat=True))
    assert "streams.stream.target_tps_changed" in actions


@pytest.mark.django_db
def test_patch_target_tps_quota_capped(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    # Free per-stream cap is 50; PATCH to 60 is rejected at command time (INV-TEN-5).
    sid = _started_running(client, stream_ws)
    resp = client.patch(f"/api/v1/streams/{sid}", {"target_tps": 60}, format="json")
    assert resp.status_code == 403, resp.content
    assert resp.json()["quota"] == "per_stream_tps"
    assert resp.json()["limit"] == 50
    assert Stream.all_objects.get(id=sid).target_tps == 50  # unchanged


@pytest.mark.django_db
def test_patch_target_tps_out_of_range_is_400(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)
    over = client.patch(f"/api/v1/streams/{sid}", {"target_tps": 2000}, format="json")
    assert over.status_code == 400, over.content
    under = client.patch(f"/api/v1/streams/{sid}", {"target_tps": 0}, format="json")
    assert under.status_code == 400, under.content


@pytest.mark.django_db
def test_patch_target_tps_idempotent_noop(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)  # target_tps=50
    before = Stream.all_objects.get(id=sid).updated_at
    resp = client.patch(f"/api/v1/streams/{sid}", {"target_tps": 50}, format="json")
    assert resp.status_code == 200
    # Re-issuing the current desired value is a silent no-op (no write).
    assert Stream.all_objects.get(id=sid).updated_at == before


@pytest.mark.django_db
def test_patch_immutable_field_is_400_immutable_code(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)
    resp = client.patch(f"/api/v1/streams/{sid}", {"seed": "99"}, format="json")
    assert resp.status_code == 400, resp.content
    body = resp.json()
    assert body["errors"][0]["code"] == "immutable_field"
    assert body["errors"][0]["field"] == "seed"


@pytest.mark.django_db
def test_patch_name_is_mutable(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    sid = _started_running(client, stream_ws)
    resp = client.patch(f"/api/v1/streams/{sid}", {"name": "renamed-run"}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["name"] == "renamed-run"
