"""JWT issuance, rotation, reuse-family revocation, and revoke-all (security §3.1)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from config.problems import AuthenticationRequired
from identity.application import auth
from identity.infra.jwt import issue_token_pair

pytestmark = pytest.mark.django_db


def test_access_token_carries_is_verified_no_workspace_claim(verified_user: Any) -> None:
    refresh = issue_token_pair(verified_user)
    access = refresh.access_token
    assert access["is_verified"] is True
    assert str(access["sub"]) == str(verified_user.id)
    # security §3.1.2: NO workspace claim.
    assert "workspace" not in access.payload
    assert "workspace_id" not in access.payload
    assert "workspace_ids" not in access.payload


def test_rotation_blacklists_old_and_issues_new(verified_user: Any) -> None:
    original = issue_token_pair(verified_user)
    new_refresh, _user = auth.rotate_refresh(str(original))
    # The presented (old) token is blacklisted; the new one differs.
    assert str(new_refresh) != str(original)
    assert BlacklistedToken.objects.filter(token__jti=original["jti"]).exists()


def test_reuse_of_rotated_token_revokes_family_and_401(verified_user: Any) -> None:
    original = issue_token_pair(verified_user)
    # Also mint a second live session for the same user, to prove family revocation.
    other = issue_token_pair(verified_user)
    auth.rotate_refresh(str(original))  # original now blacklisted

    with mock.patch("identity.application.auth.emit") as emit:
        with pytest.raises(AuthenticationRequired):
            auth.rotate_refresh(str(original))  # replay (SEC-AUTH-9)
        emit.assert_called_once()
        assert emit.call_args.args[0] == "identity.auth.refresh_reused"

    # Family revocation: every outstanding refresh for the user is blacklisted,
    # including the unrelated 'other' session.
    assert BlacklistedToken.objects.filter(token__jti=other["jti"]).exists()
    assert OutstandingToken.objects.filter(user=verified_user).exists()


def test_logout_blacklists_presented_token(verified_user: Any) -> None:
    refresh = issue_token_pair(verified_user)
    auth.logout(str(refresh))
    assert BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists()


def test_logout_tolerates_invalid_token() -> None:
    # Idempotent: an unparseable token does not raise.
    auth.logout("not-a-real-token")


def test_revoke_all_except_current_spares_one(verified_user: Any) -> None:
    keep = issue_token_pair(verified_user)
    drop = issue_token_pair(verified_user)
    revoked = auth.revoke_all_refresh_tokens(verified_user, except_jti=keep["jti"])
    assert revoked >= 1
    assert not BlacklistedToken.objects.filter(token__jti=keep["jti"]).exists()
    assert BlacklistedToken.objects.filter(token__jti=drop["jti"]).exists()


def test_rotate_invalid_token_raises_401() -> None:
    with pytest.raises(AuthenticationRequired):
        auth.rotate_refresh("garbage.token.value")
