"""Shared fixtures for the Identity test suite."""

from __future__ import annotations

from typing import Any, Protocol

import pytest
from rest_framework.test import APIClient

from identity.domain.models import User


class UserFactory(Protocol):
    def __call__(
        self, email: str = ..., *, is_verified: bool = ..., **extra: Any
    ) -> User: ...


@pytest.fixture
def api() -> APIClient:
    return APIClient()


@pytest.fixture
def password() -> str:
    # >= 10 chars, not in the common-password denylist, dissimilar to email.
    return "correct-horse-battery"


@pytest.fixture
def make_user(db: Any, password: str) -> UserFactory:
    """Factory: create a (by default verified) user."""

    def _make(email: str = "ada@example.com", *, is_verified: bool = True, **extra: Any) -> User:
        user = User.objects.create_user(email=email, password=password, **extra)
        if is_verified and not user.is_verified:
            user.is_verified = True
            user.save(update_fields=["is_verified"])
        return user

    return _make


@pytest.fixture
def verified_user(make_user: UserFactory) -> User:
    return make_user("ada@example.com", is_verified=True)


@pytest.fixture
def unverified_user(make_user: UserFactory) -> User:
    return make_user("unverified@example.com", is_verified=False)
