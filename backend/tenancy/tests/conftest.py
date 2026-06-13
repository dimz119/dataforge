"""Shared fixtures for the Tenancy test suite.

A two-workspace fixture (the TEN attack-suite shape, testing §7.1): Workspace A
(victim) and Workspace B (attacker), each with its own admin user, so the
404-not-403 cross-tenant behaviour and scoped-manager isolation can be asserted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pytest
from rest_framework.test import APIClient

from identity.domain.models import User
from identity.infra.jwt import issue_token_pair
from tenancy.application import services
from tenancy.domain.context import workspace_context
from tenancy.domain.models import Workspace


class UserFactory(Protocol):
    def __call__(self, email: str = ..., *, is_verified: bool = ...) -> User: ...


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def password() -> str:
    return "correct-horse-battery"


@pytest.fixture
def make_user(db: Any, password: str) -> UserFactory:
    def _make(email: str = "ada@example.com", *, is_verified: bool = True) -> User:
        user = User.objects.create_user(email=email, password=password)
        if is_verified and not user.is_verified:
            user.is_verified = True
            user.save(update_fields=["is_verified"])
        return user

    return _make


@dataclass
class WorkspaceSetup:
    """A workspace plus its admin user (the TEN fixture unit)."""

    workspace: Workspace
    admin: User


class WorkspaceFactory(Protocol):
    def __call__(self, email: str | None = ..., name: str = ...) -> WorkspaceSetup: ...


@pytest.fixture
def make_workspace(make_user: UserFactory) -> WorkspaceFactory:
    counter = {"n": 0}

    def _make(email: str | None = None, name: str = "Lab") -> WorkspaceSetup:
        counter["n"] += 1
        admin = make_user(email or f"admin{counter['n']}@example.com", is_verified=True)
        workspace = services.create_workspace(user=admin, name=f"{name} {counter['n']}", slug=None)
        return WorkspaceSetup(workspace=workspace, admin=admin)

    return _make


@pytest.fixture
def workspace_a(make_workspace: WorkspaceFactory) -> WorkspaceSetup:
    return make_workspace("alice@example.com", "Workspace A")


@pytest.fixture
def workspace_b(make_workspace: WorkspaceFactory) -> WorkspaceSetup:
    return make_workspace("bob@example.com", "Workspace B")


@pytest.fixture
def armed_a(workspace_a: WorkspaceSetup) -> Any:
    """Arm the active workspace context for Workspace A (for scoped-manager tests)."""
    with workspace_context(workspace_a.workspace.id):
        yield workspace_a


def auth(client: APIClient, user: User) -> APIClient:
    """Attach a Bearer access token for ``user`` to ``client``."""
    token = issue_token_pair(user)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
    return client
