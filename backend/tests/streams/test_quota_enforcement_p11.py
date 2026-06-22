"""Quota enforcement — events/day pause, idle auto-pause, admission (phase-11 exit #4).

Exit criterion #4: *"Quota exhaustion pauses streams gracefully: paused_quota in API +
UI, data intact, resume guarded on headroom; idle auto-pause emits audit + one-click
resume."* Plus the platform-protection admission control (scaling §5): Σ provisioned
``target_tps`` over the budget → ``503`` + ``Retry-After: 300``.

Covered here (the OPS-9/10 class, control-plane half):

* **events/day exhaustion → paused_quota** via ``services.system_pause(reason="quota")``:
  desired paused, lifecycle pausing, ``status`` renders ``paused_quota``, the audit
  ``streams.stream.system_paused {reason: quota}`` is written, the stream row (data)
  is preserved (NEVER deleted, INV-TEN-5), and ``df_quota_pauses_total{reason=quota}``
  increments;
* **resume rejected until headroom (T7)** — a quota-paused stream over its events/day
  cap cannot resume (403 ``events_per_day``); once the day meter falls under the cap it
  resumes;
* **idle auto-pause → paused_idle** via the beat task: audit + one-click resume (no
  headroom guard for an idle pause);
* **admission control** — a start that would push Σ provisioned target_tps over the
  budget returns 503 + Retry-After:300.

Owner lane (the conftest publishes a global ecommerce scenario). Live Redis for the
events/day meter; each meter test cleans its own bucket.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import redis
from django.conf import settings
from rest_framework.test import APIClient

from audit.domain.models import AuditLog
from observation.infra import metrics
from streams.application import metering, services
from streams.application.quotas import events_per_day_cap
from streams.domain.models import (
    LC_PAUSED,
    LC_PAUSING,
    LC_RUNNING,
    REASON_QUOTA,
    RUN_PAUSED,
    Stream,
)
from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture, **kwargs: object) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws, **kwargs), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


def _started_running(client: APIClient, stream_ws: StreamWorkspaceFixture, **kw: object) -> str:
    sid = _create(client, stream_ws, **kw)
    assert client.post(f"/api/v1/streams/{sid}/start", format="json").status_code == 200
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_RUNNING)
    return sid


@pytest.fixture
def clean_day_bucket(stream_ws: StreamWorkspaceFixture) -> Iterator[str]:
    """Clean the workspace's events/day Redis bucket before + after the test."""
    ws_id = str(stream_ws.workspace.id)
    client = redis.Redis.from_url(settings.REDIS_URL)
    client.delete(metering.day_bucket_key(ws_id))
    yield ws_id
    client.delete(metering.day_bucket_key(ws_id))


# --- events/day exhaustion → paused_quota (OPS-9) ---------------------------


@pytest.mark.django_db
def test_quota_exhaustion_pauses_to_paused_quota_with_audit(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    """system_pause(reason=quota) → paused_quota + audit + metric, data preserved."""
    before = metrics.quota_pauses_total.labels(reason="quota")._value.get()
    sid = _started_running(client, stream_ws)
    stream = Stream.all_objects.get(id=sid)

    services.system_pause(stream=stream, reason="quota")

    refreshed = Stream.all_objects.get(id=sid)
    assert refreshed.desired_state == RUN_PAUSED
    assert refreshed.lifecycle_state == LC_PAUSING  # nudge → pausing; runner converges (T6)
    assert refreshed.status_reason == REASON_QUOTA
    # Simulate the runner converging to the terminal paused state; the API then renders
    # the system reason as paused_quota (status_reason survives the convergence).
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_PAUSED)
    assert client.get(f"/api/v1/streams/{sid}").json()["status"] == "paused_quota"
    # Audit written with reason=quota.
    quota_audit = AuditLog.objects.filter(
        target_id=sid, action="streams.stream.system_paused"
    ).first()
    assert quota_audit is not None
    assert quota_audit.metadata.get("reason") == "quota"
    # Data preserved — the row still exists (NEVER deleted, INV-TEN-5).
    assert Stream.all_objects.filter(id=sid).exists()
    # The metric incremented.
    after = metrics.quota_pauses_total.labels(reason="quota")._value.get()
    assert after == before + 1


@pytest.mark.django_db
def test_system_pause_is_idempotent(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    """Re-pausing an already paused-desired stream is a no-op (INV-STR-3)."""
    sid = _started_running(client, stream_ws)
    stream = Stream.all_objects.get(id=sid)
    services.system_pause(stream=stream, reason="quota")
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_PAUSED)
    transition = Stream.all_objects.get(id=sid).last_transition_at
    services.system_pause(stream=Stream.all_objects.get(id=sid), reason="quota")
    assert Stream.all_objects.get(id=sid).last_transition_at == transition


# --- resume guarded on headroom (T7) ----------------------------------------


@pytest.mark.django_db
def test_resume_rejected_while_over_events_per_day(
    client: APIClient, stream_ws: StreamWorkspaceFixture, clean_day_bucket: str
) -> None:
    """A quota-paused stream over its events/day cap cannot resume (T7, 403)."""
    ws_id = clean_day_bucket
    sid = _started_running(client, stream_ws)
    Stream.all_objects.filter(id=sid).update(
        lifecycle_state=LC_PAUSED, desired_state=RUN_PAUSED, status_reason=REASON_QUOTA
    )
    # Push the day meter at/over the cap so the headroom guard trips.
    cap = events_per_day_cap(ws_id)
    metering.incr_events_today(ws_id, cap + 1)

    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 403, resp.content
    assert resp.json()["quota"] == "events_per_day"
    assert resp.json()["limit"] == cap
    # Still paused — the guard did not let it resume.
    assert Stream.all_objects.get(id=sid).desired_state == RUN_PAUSED


@pytest.mark.django_db
def test_resume_allowed_once_headroom_restored(
    client: APIClient, stream_ws: StreamWorkspaceFixture, clean_day_bucket: str
) -> None:
    """Once the day meter is back under the cap, the quota-paused stream resumes."""
    sid = _started_running(client, stream_ws)
    Stream.all_objects.filter(id=sid).update(
        lifecycle_state=LC_PAUSED, desired_state=RUN_PAUSED, status_reason=REASON_QUOTA
    )
    # clean_day_bucket leaves the meter at 0 → under cap → headroom available.
    resp = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["desired_state"]["run_state"] == "running"


# --- idle auto-pause → paused_idle (OPS-10) ---------------------------------


@pytest.mark.django_db
def test_idle_auto_pause_sets_paused_idle_with_audit_and_one_click_resume(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    """The idle beat task pauses a stale running stream → paused_idle + audit + resume."""
    from datetime import timedelta

    from django.utils import timezone

    from streams.tasks.idle import idle_auto_pause

    sid = _started_running(client, stream_ws)
    # Force the running stream's transition floor well past the idle threshold and clear
    # any last-event so _is_idle deems it idle (no recent delivery).
    Stream.all_objects.filter(id=sid).update(
        last_transition_at=timezone.now() - timedelta(hours=48)
    )

    result = idle_auto_pause()
    assert result["paused"] >= 1

    refreshed = Stream.all_objects.get(id=sid)
    assert refreshed.status_reason == "idle"
    # Runner converges to the terminal paused state; the API then renders paused_idle.
    Stream.all_objects.filter(id=sid).update(lifecycle_state=LC_PAUSED)
    assert client.get(f"/api/v1/streams/{sid}").json()["status"] == "paused_idle"
    idle_audit = AuditLog.objects.filter(
        target_id=sid, action="streams.stream.system_paused"
    ).first()
    assert idle_audit is not None
    assert idle_audit.metadata.get("reason") == "idle"
    # One-click resume: an idle pause has no headroom guard — resume succeeds.
    resume = client.post(f"/api/v1/streams/{sid}/resume", format="json")
    assert resume.status_code == 200, resume.content


# --- admission control (platform protection, scaling §5) --------------------


@pytest.mark.django_db
def test_admission_denied_returns_503_with_retry_after(
    client: APIClient, stream_ws: StreamWorkspaceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start over the platform admission budget → 503 + Retry-After:300 (scaling §5)."""
    # Shrink the platform budget so a single Free-tier start (target_tps 50) exceeds it.
    monkeypatch.setattr(metering, "ADMISSION_CAPACITY_EPS", 10)
    monkeypatch.setattr(metering, "ADMISSION_HEADROOM_FRACTION", 0.70)  # budget = 7 eps

    sid = _create(client, stream_ws, target_tps=50)
    resp = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert resp.status_code == 503, resp.content
    assert resp.headers.get("Retry-After") == "300"
    body = resp.json()
    assert body["status"] == 503


@pytest.mark.django_db
def test_admission_allows_start_within_budget(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    """A start within the default budget (3500*0.7=2450 eps) is admitted (control case)."""
    sid = _create(client, stream_ws, target_tps=50)
    resp = client.post(f"/api/v1/streams/{sid}/start", format="json")
    assert resp.status_code == 200, resp.content
