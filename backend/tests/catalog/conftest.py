"""DB-backed fixtures for the catalog + publish-transaction tests (Phase 3).

The validator unit tests in this package need no DB; these fixtures are opt-in
(a test only triggers them by naming them). ``published_ecommerce`` runs the real
``sync_builtin_scenarios`` path against the builtin YAML so the publish
transaction, derivation, and registration are exercised end to end.
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
def builtin_text() -> str:
    """The raw ecommerce 1.0.0 builtin manifest YAML."""
    return _BUILTIN.read_text(encoding="utf-8")


@pytest.fixture
def published_ecommerce(db: Any, builtin_text: str) -> Any:
    """Publish the ecommerce builtin (global, NULL workspace) and return the result.

    Mirrors ``sync_builtin_scenarios``: ingest a draft + run the publish
    transaction (derive + register v1 for every subset subject, R-DER).
    """
    from catalog.application import ingest, publish

    draft = ingest.create_draft(
        builtin_text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )
    return publish.publish_manifest_version(draft, actor="system", workspace_id=None)


@dataclass
class AuthedWorkspace:
    """A workspace + its admin user + a console JWT-authenticated client."""

    workspace: Any
    admin: Any
    client: APIClient


@pytest.fixture
def authed_workspace(db: Any) -> AuthedWorkspace:
    """A verified admin in a fresh workspace with a JWT-bearing APIClient."""
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair
    from tenancy.application import services as tenancy_services

    admin = User.objects.create_user(email="cat-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Catalog Lab", slug=None)
    token = issue_token_pair(admin)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return AuthedWorkspace(workspace=workspace, admin=admin, client=client)
