"""Lease-expiry watchdog tests (backend-architecture §7.1; domain-model §4.3 T4/T11).

A stream stuck in 'starting' with no live lease past the 60 s failover window →
failed (status_reason = error). A stream with a live lease (failover in progress, or
healthy) is left alone. The lease-presence read is monkeypatched so the test does
not depend on a live Redis (the watchdog's own fail-safe returns True on a degraded
cache, which is correct production behavior but the wrong thing to assert here).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from rest_framework.test import APIClient

from streams.domain.models import LC_FAILED, REASON_ERROR, Stream
from streams.infra import leases
from streams.tasks import watchdog
from tests.streams.conftest import StreamWorkspaceFixture, create_body


def _create_and_start(client: APIClient, stream_ws: StreamWorkspaceFixture) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws), format="json")
    assert resp.status_code == 201, resp.content
    sid = str(resp.json()["stream_id"])
    assert client.post(f"/api/v1/streams/{sid}/start", format="json").status_code == 200
    return sid


def _age_transition(stream_id: str, *, seconds: int) -> None:
    """Backdate last_transition_at so the stream is past the failover window."""
    from django.utils import timezone

    Stream.all_objects.filter(id=stream_id).update(
        last_transition_at=timezone.now() - timedelta(seconds=seconds)
    )


@pytest.mark.django_db
def test_t4_no_lease_within_window_fails(
    client: APIClient,
    stream_ws: StreamWorkspaceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: False)
    sid = _create_and_start(client, stream_ws)  # lifecycle = starting
    _age_transition(sid, seconds=90)  # past the 60 s window

    result = watchdog.lease_expiry_watchdog()
    assert result["failed"] == 1
    stream = Stream.all_objects.get(id=sid)
    assert stream.lifecycle_state == LC_FAILED
    assert stream.status_reason == REASON_ERROR


@pytest.mark.django_db
def test_live_lease_is_not_failed(
    client: APIClient,
    stream_ws: StreamWorkspaceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live lease (healthy / failover-in-progress): the watchdog leaves it alone.
    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: True)
    sid = _create_and_start(client, stream_ws)
    _age_transition(sid, seconds=90)

    result = watchdog.lease_expiry_watchdog()
    assert result["failed"] == 0
    assert Stream.all_objects.get(id=sid).lifecycle_state != LC_FAILED


@pytest.mark.django_db
def test_within_window_not_failed(
    client: APIClient,
    stream_ws: StreamWorkspaceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No lease yet, but still inside the 60 s window (failover may yet succeed).
    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: False)
    sid = _create_and_start(client, stream_ws)
    _age_transition(sid, seconds=10)  # well inside the window

    result = watchdog.lease_expiry_watchdog()
    assert result["failed"] == 0
    assert Stream.all_objects.get(id=sid).lifecycle_state != LC_FAILED


@pytest.mark.django_db
def test_overdue_streams_query(
    client: APIClient,
    stream_ws: StreamWorkspaceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: False)
    sid = _create_and_start(client, stream_ws)
    _age_transition(sid, seconds=90)
    overdue = watchdog.overdue_streams()
    assert [str(s.id) for s in overdue] == [sid]
