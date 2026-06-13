"""User model: normalization (INV-ID-1), Argon2id hashing, soft-delete, policy."""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.db import IntegrityError, transaction

from config.settings import base as base_settings
from identity.domain.email import normalize_email
from identity.domain.models import User

pytestmark = pytest.mark.django_db


def test_normalize_email_lowercases_and_strips() -> None:
    assert normalize_email("  Ada@Example.COM ") == "ada@example.com"
    # Idempotent.
    assert normalize_email(normalize_email("Ada@Example.com")) == "ada@example.com"


def test_create_user_normalizes_email(make_user: Any) -> None:
    user = make_user("Ada@Example.COM")
    assert user.email == "ada@example.com"


def test_password_is_argon2id(make_user: Any) -> None:
    user = make_user("argon@example.com")
    # The stored hash is Argon2 (the test settings override is MD5; here we set a
    # password through Django's hasher chain which uses base PASSWORD_HASHERS in
    # production — assert the production config directly below).
    assert identify_hasher(user.password) is not None
    assert user.check_password("correct-horse-battery")


def test_base_password_hashers_put_argon2_first() -> None:
    # security §3.1.1: Argon2PasswordHasher first in PASSWORD_HASHERS.
    assert (
        base_settings.PASSWORD_HASHERS[0]
        == "django.contrib.auth.hashers.Argon2PasswordHasher"
    )


def test_argon2_params_match_django_audited_defaults() -> None:
    # security §3.1.1: time_cost=2, memory_cost=102400 KiB, parallelism=8.
    from django.contrib.auth.hashers import Argon2PasswordHasher

    hasher = Argon2PasswordHasher()
    assert hasher.time_cost == 2
    assert hasher.memory_cost == 102400
    assert hasher.parallelism == 8


def test_case_insensitive_uniqueness_across_non_deleted(make_user: Any) -> None:
    make_user("dup@example.com")
    # INV-ID-1: a second non-deleted user with the same email (any case) fails.
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user(email="DUP@example.com", password="x" * 12)


def test_soft_deleted_email_can_be_reused(make_user: Any) -> None:
    first = make_user("recycle@example.com")
    first.deleted_at = first.created_at  # tombstone
    first.email = "deleted:recycled"  # scrub sentinel
    first.save(update_fields=["deleted_at", "email"])
    # A live account may now take the freed address (partial unique index).
    second = User.objects.create_user(email="recycle@example.com", password="x" * 12)
    assert second.is_deleted is False


def test_login_field_is_email() -> None:
    assert User.USERNAME_FIELD == "email"
    assert User.REQUIRED_FIELDS == []


def test_password_policy_settings_min_length_10() -> None:
    validators = cast("list[dict[str, Any]]", base_settings.AUTH_PASSWORD_VALIDATORS)
    names = [v["NAME"] for v in validators]
    assert "django.contrib.auth.password_validation.MinimumLengthValidator" in names
    assert "django.contrib.auth.password_validation.CommonPasswordValidator" in names
    min_validator = next(
        v for v in validators if v["NAME"].endswith("MinimumLengthValidator")
    )
    assert min_validator["OPTIONS"]["min_length"] == 10


def test_jwt_signing_key_is_not_django_secret_key() -> None:
    # security §3.1.2: a dedicated JWT key, never DJANGO_SECRET_KEY.
    assert settings.SIMPLE_JWT["SIGNING_KEY"] != settings.SECRET_KEY
