"""DB-backed fixtures for the Stream Control tests (Phase 5).

Builds a workspace + verified admin + a pinned ecommerce scenario instance (small
catalogs so any downstream generation stays fast), context armed, plus a
JWT-authenticated API client. Mirrors the generation conftest: the workspace
creation seeds the Free-tier quota row, so the concurrent-stream + per-stream TPS
caps are real.

These fixtures run in the publish-path (owner) lane (a global scenario is seeded);
RLS-negative cross-tenant assertions live in the permanent TEN suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from rest_framework.test import APIClient

_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)
SMALL_CATALOGS = {"catalog_sizes": {"users": 100, "products": 50}}


@dataclass
class StreamWorkspaceFixture:
    """A workspace + admin + a pinned scenario instance, context-armed."""

    workspace: Any
    admin: Any
    instance: Any


def _publish_ecommerce() -> Any:
    from catalog.application import ingest, publish

    text = _BUILTIN.read_text(encoding="utf-8")
    draft = ingest.create_draft(
        text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )
    return publish.publish_manifest_version(draft, actor="system", workspace_id=None)


@pytest.fixture
def stream_ws(db: Any) -> StreamWorkspaceFixture:
    """A workspace with a pinned ecommerce instance (small catalogs), context armed."""
    from catalog.application import services as catalog_services
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    _publish_ecommerce()
    admin = User.objects.create_user(email="stream-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Stream Lab", slug=None)
    ws_context.activate(workspace.id)
    instance = catalog_services.create_instance(
        workspace=workspace,
        name="ecommerce-stream",
        scenario_slug="ecommerce",
        manifest_version="1.0.0",
        configuration=SMALL_CATALOGS,
        default_seed=42,
        actor=admin,
    )
    return StreamWorkspaceFixture(workspace=workspace, admin=admin, instance=instance)


@pytest.fixture
def client(stream_ws: StreamWorkspaceFixture) -> APIClient:
    """A JWT-authenticated API client for the workspace admin."""
    from identity.infra.jwt import issue_token_pair

    token = issue_token_pair(stream_ws.admin)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return api


def create_body(stream_ws: StreamWorkspaceFixture, **kwargs: Any) -> dict[str, Any]:
    """A valid POST /streams body for the fixture's workspace + instance."""
    body: dict[str, Any] = {
        "workspace_id": str(stream_ws.workspace.id),
        "scenario_instance_id": str(stream_ws.instance.id),
        "name": "dedup-101-run-1",
        "seed": "424242424242",
        "target_tps": 50,
    }
    body.update(kwargs)
    return body
