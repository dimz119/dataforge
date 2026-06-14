"""DB fixtures for the WebSocket tail consumer tests (delivery-channels §6).

Builds the minimal world the WS handshake needs: a workspace + verified admin, an
API key scoped ``events:read``, and a ``Stream`` row owned by the workspace (the
consumer resolves the URL stream's owning workspace, then gates on the credential —
§6.2). The Redis revocation cache is faked in-memory so the PR lane runs on SQLite
without live Redis (mirrors ``tests/tenancy/conftest._fake_revocation_cache``); the
connection registry + channel layer use the in-memory backends configured in
``config.settings.test``.

A second ("foreign") workspace + key lets the cross-tenant 4404 probe present a
valid-but-foreign credential against the victim's stream.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from identity.domain.models import User
from tenancy.application import keys as key_service
from tenancy.application import services as tenancy_services
from tenancy.application.services import worker_workspace_scope
from tenancy.domain.models import SCOPE_EVENTS_READ, Workspace

__all__ = ["WsWorld", "build_ws_world"]


@dataclass(frozen=True)
class WsWorld:
    """A workspace + admin + an events:read key + a stream the consumer can resolve."""

    workspace: Workspace
    admin: User
    api_key_plaintext: str
    api_key_prefix: str
    stream_id: str


def _create_stream(*, workspace: Workspace, admin: User) -> uuid.UUID:
    """A minimal owned ``Stream`` row (only ownership resolution is exercised)."""
    from streams.domain.models import Stream

    stream_id = uuid.uuid4()
    # worker_workspace_scope arms BOTH the contextvar (Layer 1) AND the Postgres
    # ``app.workspace_id`` GUC (Layer 2) inside one transaction, so the Class-T WITH
    # CHECK admits the INSERT under the NOBYPASSRLS role in the Postgres lane (a bare
    # contextvar passes the scoped manager but RLS hides/blocks the row). No-op for
    # RLS on SQLite, where the same fixtures run in the fast unit lane.
    with worker_workspace_scope(workspace.id):
        Stream.objects.create(
            id=stream_id,
            workspace=workspace,
            scenario_config_id=uuid.uuid4(),
            scenario_slug="ecommerce",
            name="WS Tail Lab",
            manifest_version="1.0.0",
            scenario_definition_id=uuid.uuid4(),
            seed=4242,
            virtual_epoch=datetime(2026, 6, 14, tzinfo=UTC),
            created_by=admin.id,
        )
    return stream_id


def build_ws_world(
    *, make_user: Any, label: str = "WS", scopes: list[str] | None = None
) -> WsWorld:
    """Build one fully populated WS world (workspace + admin + key + stream)."""
    admin = make_user(f"{label}@example.com", is_verified=True)
    workspace = tenancy_services.create_workspace(
        user=admin, name=f"WS {label}", slug=None
    )
    with worker_workspace_scope(workspace.id):
        api_key, plaintext = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name=f"{label}-key",
            scopes=scopes if scopes is not None else [SCOPE_EVENTS_READ],
            expires_at=None,
            actor_role="admin",
        )
    stream_id = _create_stream(workspace=workspace, admin=admin)
    return WsWorld(
        workspace=workspace,
        admin=admin,
        api_key_plaintext=plaintext,
        api_key_prefix=api_key.key_prefix,
        stream_id=str(stream_id),
    )
