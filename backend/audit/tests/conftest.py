"""Shared fixtures for the Audit test suite."""

from __future__ import annotations

from typing import Any, Protocol

import pytest

from identity.domain.models import User


class UserFactory(Protocol):
    def __call__(self, email: str = ...) -> User: ...


@pytest.fixture
def make_user(db: Any) -> UserFactory:
    counter = {"n": 0}

    def _make(email: str = "") -> User:
        counter["n"] += 1
        addr = email or f"user{counter['n']}@example.com"
        return User.objects.create_user(email=addr, password="correct-horse-battery")

    return _make
