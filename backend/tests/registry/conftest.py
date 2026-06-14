"""DB-backed fixtures for the registry tests (Phase 3).

``published_ecommerce`` publishes the ecommerce builtin (global) so the registry
read API + registration invariants run against real derived/registered subjects.
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


@pytest.fixture
def published_ecommerce(db: Any) -> Any:
    from catalog.application import ingest, publish

    draft = ingest.create_draft(
        _BUILTIN.read_text(encoding="utf-8"),
        workspace_id=None,
        is_workspace_visibility=False,
        builtin=True,
    )
    return publish.publish_manifest_version(draft, actor="system", workspace_id=None)


@dataclass
class AuthedWorkspace:
    workspace: Any
    admin: Any
    client: APIClient


@pytest.fixture
def authed_workspace(db: Any) -> AuthedWorkspace:
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair
    from tenancy.application import services as tenancy_services

    admin = User.objects.create_user(email="reg-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Registry Lab", slug=None)
    token = issue_token_pair(admin)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return AuthedWorkspace(workspace=workspace, admin=admin, client=client)
