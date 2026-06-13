"""user_tokens: single-use, supersession, TTL, hashing (INV-ID-3)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from identity.application import tokens as token_service
from identity.domain.models import UserToken
from identity.infra import tokens as token_crypto

pytestmark = pytest.mark.django_db


def test_generate_token_returns_plaintext_and_matching_hash() -> None:
    plaintext, token_hash = token_crypto.generate_token()
    assert token_crypto.hash_token(plaintext) == token_hash
    assert token_crypto.tokens_match(plaintext, token_hash)
    assert not token_crypto.tokens_match("wrong", token_hash)


def test_issue_token_persists_only_the_hash(verified_user: Any) -> None:
    plaintext = token_service.issue_token(verified_user, UserToken.KIND_EMAIL_VERIFICATION)
    row = UserToken.objects.get(user=verified_user, kind=UserToken.KIND_EMAIL_VERIFICATION)
    # Plaintext is never stored.
    assert row.token_hash != plaintext
    assert row.token_hash == token_crypto.hash_token(plaintext)


def test_issue_supersedes_prior_unconsumed_tokens(verified_user: Any) -> None:
    first = token_service.issue_token(verified_user, UserToken.KIND_PASSWORD_RESET)
    second = token_service.issue_token(verified_user, UserToken.KIND_PASSWORD_RESET)
    # INV-ID-3 supersession: the first is now consumed and no longer usable.
    assert token_service.consume_token(first, UserToken.KIND_PASSWORD_RESET) is None
    assert token_service.consume_token(second, UserToken.KIND_PASSWORD_RESET) is not None


def test_supersession_is_kind_scoped(verified_user: Any) -> None:
    verify = token_service.issue_token(verified_user, UserToken.KIND_EMAIL_VERIFICATION)
    # Issuing a reset token must not supersede a verification token.
    token_service.issue_token(verified_user, UserToken.KIND_PASSWORD_RESET)
    assert token_service.consume_token(verify, UserToken.KIND_EMAIL_VERIFICATION) is not None


def test_consume_is_single_use(verified_user: Any) -> None:
    plaintext = token_service.issue_token(verified_user, UserToken.KIND_EMAIL_VERIFICATION)
    assert token_service.consume_token(plaintext, UserToken.KIND_EMAIL_VERIFICATION) is not None
    # Second consumption fails — single-use (INV-ID-3).
    assert token_service.consume_token(plaintext, UserToken.KIND_EMAIL_VERIFICATION) is None


def test_consume_rejects_expired_token(verified_user: Any) -> None:
    plaintext = token_service.issue_token(verified_user, UserToken.KIND_PASSWORD_RESET)
    row = UserToken.objects.get(user=verified_user, kind=UserToken.KIND_PASSWORD_RESET)
    row.expires_at = timezone.now() - timedelta(seconds=1)
    row.save(update_fields=["expires_at"])
    assert token_service.consume_token(plaintext, UserToken.KIND_PASSWORD_RESET) is None


def test_consume_rejects_wrong_kind(verified_user: Any) -> None:
    plaintext = token_service.issue_token(verified_user, UserToken.KIND_EMAIL_VERIFICATION)
    # A verification token cannot be consumed as a reset token.
    assert token_service.consume_token(plaintext, UserToken.KIND_PASSWORD_RESET) is None


def test_ttls_are_24h_verification_and_1h_reset(verified_user: Any) -> None:
    before = timezone.now()
    token_service.issue_token(verified_user, UserToken.KIND_EMAIL_VERIFICATION)
    token_service.issue_token(verified_user, UserToken.KIND_PASSWORD_RESET)
    verify = UserToken.objects.get(
        user=verified_user, kind=UserToken.KIND_EMAIL_VERIFICATION
    )
    reset = UserToken.objects.get(user=verified_user, kind=UserToken.KIND_PASSWORD_RESET)
    # ~24 h and ~1 h, allowing a small epsilon for the elapsed test time.
    epsilon = timedelta(minutes=1)
    assert abs((verify.expires_at - before) - timedelta(hours=24)) < epsilon
    assert abs((reset.expires_at - before) - timedelta(hours=1)) < epsilon
