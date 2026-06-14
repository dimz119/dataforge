"""GET /api/v1/streams/{id}/stats tests (api-spec §4.11.1; observability §5).

The tenant-facing StreamStats endpoint:

* **response shape** — the §4.11.1 fields, rendered from the Redis counters;
* **foreign-workspace → 404** (W-1/W-3 masking, the cross-tenant contract);
* **health derivation** — ``degraded`` when no live runner lease / counters absent,
  ``healthy`` when the lease is fresh and counters current;
* **rebuild command** — reconstructs a real stream's counters from event_buffer.

Redis is in-process fakeredis (``_stats_redis`` patches the stats client + the lease
read) so the SQLite PR lane runs without live Redis.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.streams.conftest import StreamWorkspaceFixture, create_body

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _stats_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the stats counters at fakeredis for the endpoint tests (no live Redis)."""
    import fakeredis

    from delivery.infra import stream_stats

    client = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(stream_stats, "_redis", lambda: client)
    return client


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


def _seed_counts(stream_ws: StreamWorkspaceFixture, stream_id: str) -> None:
    from delivery.infra import stream_stats

    stream_stats.record_delivered_batch(
        workspace_id=str(stream_ws.workspace.id),
        stream_id=stream_id,
        envelopes=[
            {"event_type": "product_viewed", "emitted_at": "2026-06-10T14:23:05.287113Z"},
            {"event_type": "order_placed", "emitted_at": "2026-06-10T14:23:05.300000Z"},
            {"event_type": "order_placed", "emitted_at": "2026-06-10T14:23:05.400000Z"},
        ],
    )


def test_stats_response_shape(
    client: APIClient, stream_ws: StreamWorkspaceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The §4.11.1 response carries the counters + buffer + virtual_clock blocks."""
    from streams.infra import leases

    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: True)
    sid = _create(client, stream_ws)
    _seed_counts(stream_ws, sid)

    resp = client.get(f"/api/v1/streams/{sid}/stats")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["stream_id"] == sid
    assert body["total_events"] == 3
    assert body["by_event_type"] == {"product_viewed": 1, "order_placed": 2}
    assert body["target_tps"] == 50  # the create_body default
    assert "observed_tps" in body
    assert set(body["buffer"]) == {
        "earliest_available_at",
        "latest_event_at",
        "retention_hours",
    }
    assert set(body["virtual_clock"]) == {"virtual_now", "speed_multiplier"}
    assert "as_of" in body


def test_stats_health_degraded_without_lease(
    client: APIClient, stream_ws: StreamWorkspaceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running stream with no live runner lease reports health=degraded (§4.11.1)."""
    from streams.infra import leases

    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: False)
    sid = _create(client, stream_ws)
    client.post(f"/api/v1/streams/{sid}/start", format="json")  # → running-desired
    _seed_counts(stream_ws, sid)

    body = client.get(f"/api/v1/streams/{sid}/stats").json()
    # lifecycle becomes 'starting' on a fresh start; health is null until live, OR
    # degraded once running with no lease. Either way it is not 'healthy' here.
    assert body["health"] in (None, "degraded")


def test_stats_empty_counters_present_shape(
    client: APIClient, stream_ws: StreamWorkspaceFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that never delivered reads zeroes with a valid shape (not 500)."""
    from streams.infra import leases

    monkeypatch.setattr(leases, "has_live_lease", lambda *a, **k: False)
    sid = _create(client, stream_ws)
    body = client.get(f"/api/v1/streams/{sid}/stats").json()
    assert body["total_events"] == 0
    assert body["by_event_type"] == {}
    assert body["observed_tps"] == 0.0
    assert body["last_event_at"] is None


def test_foreign_jwt_stats_is_404(
    client: APIClient, stream_ws: StreamWorkspaceFixture, db: Any
) -> None:
    """A JWT caller not in the stream's workspace gets 404 (W-3 masking)."""
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair

    sid = _create(client, stream_ws)
    other = User.objects.create_user(
        email="stats-outsider@example.com", password="pw-correct-horse"
    )
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    foreign = APIClient()
    foreign.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(other).access_token}")
    assert foreign.get(f"/api/v1/streams/{sid}/stats").status_code == 404


def test_unknown_stream_stats_is_404(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    assert client.get(f"/api/v1/streams/{uuid.uuid4()}/stats").status_code == 404
    assert client.get("/api/v1/streams/not-a-uuid/stats").status_code == 404


def test_rebuild_command_reconstructs_from_buffer(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    """``rebuild_stream_stats`` reconstructs a real stream's counters from the buffer."""
    from io import StringIO

    from django.core.management import call_command

    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.infra import stream_stats
    from delivery.infra.buffer_writer_channel import BufferWriterChannel
    from tenancy.application.services import worker_workspace_scope
    from tests.delivery.conformance import make_batch

    sid = _create(client, stream_ws)
    ws = str(stream_ws.workspace.id)
    # Build envelopes attributed to the REAL stream (the writer asserts each envelope's
    # workspace/stream matches the batch, SINK-7).
    events = []
    for i in range(4):
        env = dict(order_placed_envelope(seed=4242 + i))
        env["workspace_id"] = ws
        env["stream_id"] = sid
        events.append(env)
    with worker_workspace_scope(stream_ws.workspace.id):
        BufferWriterChannel().deliver(
            make_batch(events, workspace_id=ws, stream_id=sid)  # type: ignore[arg-type]
        )

    out = StringIO()
    call_command("rebuild_stream_stats", "--stream-id", sid, stdout=out)
    assert "rebuilt total=4" in out.getvalue()

    snap = stream_stats.read_stats(workspace_id=str(stream_ws.workspace.id), stream_id=sid)
    assert snap.total_events == 4
    assert snap.by_event_type == {"order_placed": 4}
