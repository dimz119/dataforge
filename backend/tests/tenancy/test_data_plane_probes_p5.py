"""TEN §7.5 (Phase 5) data-plane probes — permanent, unskippable.

The §7.5 probe-growth table adds, at Phase 5:

> REST cursor pull on A's stream with B's key → **404**; B's key with
> ``events:read`` revoked-scope variant → **403**.

The foreign-key → 404 half is already covered for *free* by the auto-enrolling
cross-tenant probe (``test_cross_tenant_probes``: the events route is classified
``SCOPE`` → foreign_key 404). What that suite cannot express is the *second* half —
the **revoked-scope** distinction: a key in its OWN workspace, against its OWN
stream, but lacking ``events:read``, must get **403 permission-denied** (scope
gate), NEVER 404 (which would mask its own resource) and NEVER 200 (which would
leak past the scope). 403-vs-404 here is the exact security §3.3 boundary: the
workspace matches, so masking does not apply; the scope is missing, so the gate
fires. This module pins both poles of the P5 row so the data-plane scope contract
cannot regress.

It is data-plane only (X-API-Key), Postgres-or-SQLite agnostic (the events read is
over ``event_buffer``, which has no RLS dependency to *reach* the 403/404 — the
permission check runs before any row read), and rides the permanent ``tenancy``
marker.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.tenancy

EVENTS_URL = "/api/v1/streams/{sid}/events"


def _key_client(key: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_API_KEY=key)
    return api


@pytest.fixture
def p5_world(db: Any) -> Any:
    """One workspace + a stream + three keys: events:read, NO events:read, foreign.

    Mirrors the cursor conftest arming so it passes RLS on the Postgres lane and runs
    hermetically on SQLite. No buffer rows are needed — the auth/scope gate fires
    before any read, which is exactly the point of these probes.
    """
    from identity.domain.models import User
    from streams.domain.models import Stream
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN

    admin = User.objects.create_user(email="p5-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="P5 Lab", slug=None)
    ws_context.activate(workspace.id)

    stream_id = str(uuid.uuid4())
    Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name="p5-stream",
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=datetime.now(UTC),
    )

    with ws_context.workspace_context(workspace.id):
        _ok, read_key = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name="events-read",
            scopes=["events:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
        _no, revoked_scope_key = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name="no-events-read",
            scopes=["streams:read"],  # the revoked-scope variant: NO events:read
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )

    # Workspace B — a fully foreign tenant with a valid events:read key.
    foreign_admin = User.objects.create_user(
        email="p5-foreign@example.com", password="pw-correct-horse"
    )
    foreign_admin.is_verified = True
    foreign_admin.save(update_fields=["is_verified"])
    foreign_ws = tenancy_services.create_workspace(user=foreign_admin, name="P5 Foreign", slug=None)
    with ws_context.workspace_context(foreign_ws.id):
        _f, foreign_key = key_service.create_key(
            workspace=foreign_ws,
            actor=foreign_admin,
            name="foreign-events-read",
            scopes=["events:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
    ws_context.activate(workspace.id)

    class World:
        pass

    world = World()
    world.stream_id = stream_id  # type: ignore[attr-defined]
    world.read_key = read_key  # type: ignore[attr-defined]
    world.revoked_scope_key = revoked_scope_key  # type: ignore[attr-defined]
    world.foreign_key = foreign_key  # type: ignore[attr-defined]
    return world


@pytest.mark.django_db
def test_p5_foreign_key_is_404(p5_world: Any) -> None:
    """B's valid events:read key on A's stream → 404 (never 403; W-1 masking)."""
    resp = _key_client(p5_world.foreign_key).get(
        EVENTS_URL.format(sid=p5_world.stream_id), {"from": "earliest"}
    )
    assert resp.status_code == 404, resp.content
    # The cardinal anti-enumeration rule: a foreign object never confirms existence.
    body = resp.json()
    assert not body.get("type", "").endswith("/permission-denied"), (
        "foreign-key access returned permission-denied (403 semantics) — must mask to 404"
    )


@pytest.mark.django_db
def test_p5_revoked_scope_is_403(p5_world: Any) -> None:
    """An own-workspace key lacking events:read on its OWN stream → 403, not 404/200.

    The workspace matches (no masking), the scope is missing (the gate fires): this is
    the precise 403 the §7.5 P5 row demands, distinct from the foreign-key 404 above.
    """
    resp = _key_client(p5_world.revoked_scope_key).get(
        EVENTS_URL.format(sid=p5_world.stream_id), {"from": "earliest"}
    )
    assert resp.status_code == 403, resp.content
    body = resp.json()
    assert body["type"].endswith("/permission-denied")
    assert body.get("required_scope") == "events:read"


@pytest.mark.django_db
def test_p5_own_scope_is_not_403_or_404(p5_world: Any) -> None:
    """The control: the same workspace's events:read key on its own stream is admitted.

    Proves the 403/404 above are the scope/workspace gates firing, not a blanket deny —
    a key WITH events:read reaches the read path (200 with an empty buffer here).
    """
    resp = _key_client(p5_world.read_key).get(
        EVENTS_URL.format(sid=p5_world.stream_id), {"from": "earliest"}
    )
    assert resp.status_code == 200, resp.content
