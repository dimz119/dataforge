"""INV-ID-2 verified-user gate: the reusable predicate/permission tenancy applies."""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIRequestFactory

from config.problems import EmailNotVerified
from identity.application.permissions import IsVerified, is_verified, require_verified

pytestmark = pytest.mark.django_db


def test_is_verified_true_for_verified_user(verified_user: Any) -> None:
    assert is_verified(verified_user) is True


def test_is_verified_false_for_unverified(unverified_user: Any) -> None:
    assert is_verified(unverified_user) is False


def test_is_verified_false_for_tombstoned(make_user: Any) -> None:
    user = make_user("gone@example.com", is_verified=True)
    user.deleted_at = user.created_at
    assert is_verified(user) is False


def test_require_verified_raises_email_not_verified(unverified_user: Any) -> None:
    with pytest.raises(EmailNotVerified):
        require_verified(unverified_user)


def test_require_verified_passes_for_verified(verified_user: Any) -> None:
    require_verified(verified_user)  # no raise


def test_permission_class_raises_403_for_unverified(unverified_user: Any) -> None:
    request = APIRequestFactory().post("/api/v1/workspaces")
    request.user = unverified_user
    with pytest.raises(EmailNotVerified):
        IsVerified().has_permission(request, view=None)  # type: ignore[arg-type]


def test_permission_class_allows_verified(verified_user: Any) -> None:
    request = APIRequestFactory().post("/api/v1/workspaces")
    request.user = verified_user
    assert IsVerified().has_permission(request, view=None) is True  # type: ignore[arg-type]
