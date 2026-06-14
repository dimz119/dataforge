"""TEN §7.5 (Phase 6) data-plane probes — permanent, unskippable.

The §7.5 probe-growth table adds, at Phase 6:

> WS connect to ``/ws/streams/{A-stream}/events`` with B's key → rejected at
> handshake (**4404** close per the subprotocol); **stats endpoints for A's stream
> → 404**.

Two poles, both pinned here as permanent ``tenancy`` gates:

* **Foreign stats → 404 (REST).** B's valid ``streams:read`` key (and B's JWT) on
  A's ``GET /streams/{id}/stats`` must mask to 404 — never 403 (which would confirm
  the stream exists), never 200 (which would leak A's counters). The foreign-key →
  404 pole is ALSO covered for free by the auto-enrolling cross-tenant probe (the
  stats route is classified ``SCOPE`` in ``access_policy``); this module pins it
  explicitly so the P6 row cannot silently regress, and adds the own-workspace
  control (an own ``streams:read`` key reaches the read path → not 404/403).
* **Foreign WS handshake → close 4404.** The handshake-level half lives with the WS
  consumer suite (``tests/delivery/test_ws_consumer.test_foreign_workspace_stream_
  closes_4404``), which drives the real consumer + close-code table; this module
  references it as the binding P6 WS gate and re-asserts the close-code constant so a
  drift in the code table fails here too.

Data-plane shaped (X-API-Key + JWT), Postgres-or-SQLite agnostic (the auth/scope/
ownership gate fires before any counter read), riding the permanent ``tenancy`` marker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.tenancy

STATS_URL = "/api/v1/streams/{sid}/stats"


def _key_client(key: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_API_KEY=key)
    return api


def _jwt_client(access: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return api


@pytest.fixture
def p6_world(db: Any) -> Any:
    """Workspace A (victim) + a stream + an own streams:read key; Workspace B
    (attacker) with a valid streams:read key + a logged-in JWT."""
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair
    from streams.domain.models import Stream
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN

    admin = User.objects.create_user(email="p6-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="P6 Lab", slug=None)
    ws_context.activate(workspace.id)

    stream_id = str(uuid.uuid4())
    Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name="p6-stream",
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=datetime.now(UTC),
    )

    with ws_context.workspace_context(workspace.id):
        _ok, own_read_key = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name="own-streams-read",
            scopes=["streams:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )

    # Workspace B — a fully foreign tenant with a valid streams:read key + a JWT.
    foreign_admin = User.objects.create_user(
        email="p6-foreign@example.com", password="pw-correct-horse"
    )
    foreign_admin.is_verified = True
    foreign_admin.save(update_fields=["is_verified"])
    foreign_ws = tenancy_services.create_workspace(user=foreign_admin, name="P6 Foreign", slug=None)
    with ws_context.workspace_context(foreign_ws.id):
        _f, foreign_key = key_service.create_key(
            workspace=foreign_ws,
            actor=foreign_admin,
            name="foreign-streams-read",
            scopes=["streams:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
    foreign_access = str(issue_token_pair(foreign_admin).access_token)
    ws_context.activate(workspace.id)

    class World:
        pass

    world = World()
    world.stream_id = stream_id  # type: ignore[attr-defined]
    world.own_read_key = own_read_key  # type: ignore[attr-defined]
    world.foreign_key = foreign_key  # type: ignore[attr-defined]
    world.foreign_access = foreign_access  # type: ignore[attr-defined]
    return world


@pytest.mark.django_db
def test_p6_foreign_key_stats_is_404(p6_world: Any) -> None:
    """B's valid streams:read key on A's stats → 404 (never 403; W-1 masking)."""
    resp = _key_client(p6_world.foreign_key).get(STATS_URL.format(sid=p6_world.stream_id))
    assert resp.status_code == 404, resp.content
    body = resp.json()
    assert not body.get("type", "").endswith("/permission-denied"), (
        "foreign-key stats returned permission-denied (403 semantics) — must mask to 404"
    )


@pytest.mark.django_db
def test_p6_foreign_jwt_stats_is_404(p6_world: Any) -> None:
    """B's console JWT on A's stats → 404 (the JWT-credential pole of the P6 row)."""
    resp = _jwt_client(p6_world.foreign_access).get(STATS_URL.format(sid=p6_world.stream_id))
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_p6_unknown_stream_stats_is_404(p6_world: Any) -> None:
    """A random stream id under a valid own key → 404, never 5xx (anti-enumeration)."""
    resp = _key_client(p6_world.own_read_key).get(STATS_URL.format(sid=str(uuid.uuid4())))
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_p6_own_key_stats_is_not_404_or_403(p6_world: Any) -> None:
    """The control: A's own streams:read key on A's stats reaches the read path (200).

    Proves the 404s above are the workspace gate firing, not a blanket deny — an own
    key WITH streams:read returns the stats response (empty counters on a fresh
    stream), so the foreign 404 is genuinely the masking boundary."""
    resp = _key_client(p6_world.own_read_key).get(STATS_URL.format(sid=p6_world.stream_id))
    assert resp.status_code == 200, resp.content
    assert "total_events" in resp.json()


def test_p6_ws_handshake_close_code_is_4404() -> None:
    """The binding WS-3 P6 close code: a foreign-workspace key on the WS handshake
    closes 4404 (asserted end-to-end against the real consumer in
    tests/delivery/test_ws_consumer.test_foreign_workspace_stream_closes_4404).

    Re-pin the constant here so a drift in the close-code table trips the TEN gate."""
    from delivery.domain.ws_protocol import CLOSE_NOT_FOUND

    assert CLOSE_NOT_FOUND == 4404
