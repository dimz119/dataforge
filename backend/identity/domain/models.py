"""Domain models for the Identity context (domain-model §2.1).

`User` is the account aggregate root (database-schema §3.1): UUIDv4 pk minted
app-side (C-3), email normalized lowercase (INV-ID-1), Argon2id password hash
(security-architecture §3.1.1), `is_verified` gate (INV-ID-2), soft-delete
tombstone `deleted_at` (INV-ID-4). `UserToken` is the single-use verification /
reset entity (database-schema §3.2, INV-ID-3).

Identity is workspace-agnostic: neither table is tenant-owned and neither
carries `workspace_id` (database-schema §9.6); they are listed in the tenancy
guard's exempt set.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from identity.domain.email import normalize_email
from identity.domain.ids import uuid4, uuid7


class UserManager(BaseUserManager["User"]):
    """Email-login manager (email is the `USERNAME_FIELD`, no usernames)."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=normalize_email(email), **extra)
        user.set_password(password)  # Argon2id via PASSWORD_HASHERS (security §3.1.1)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_verified", True)
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError("Superuser must have is_staff and is_superuser set.")
        return self._create_user(email, password, **extra)

    def get_by_natural_key(self, username: str | None) -> User:
        # username == the email; normalize so login is case-insensitive (INV-ID-1).
        return self.get(email=normalize_email(username or ""), deleted_at__isnull=True)


class User(AbstractBaseUser, PermissionsMixin):
    """Account aggregate root (database-schema §3.1; domain-model §2.1)."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    email = models.TextField()  # normalized lowercase at the boundary (INV-ID-1)
    # Django's password attribute mapped to the spec column name `password_hash`
    # (database-schema §3.1). `password` stays the attribute so set_password /
    # check_password / PASSWORD_HASHERS work unchanged.
    password = models.CharField(max_length=128, db_column="password_hash")
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # soft-delete (INV-ID-4)

    # Minimal Django auth surface (not in the §3.1 DDL but required by the admin /
    # createsuperuser / permission machinery; `users` is not tenant-owned, §9.6).
    is_staff = models.BooleanField(default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects: ClassVar[UserManager] = UserManager()

    class Meta:
        db_table = "users"  # BE-APP-1 / C-2: table name fixed by database-schema.md
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # INV-ID-1: case-insensitive uniqueness across non-deleted users
            # (database-schema §3.1 `users_email_uq`).
            models.UniqueConstraint(
                Lower("email"),
                condition=models.Q(deleted_at__isnull=True),
                name="users_email_uq",
            ),
        ]

    def __str__(self) -> str:
        return self.email

    @property
    def is_deleted(self) -> bool:
        """Soft-delete tombstone set (INV-ID-4)."""
        return self.deleted_at is not None


class UserToken(models.Model):
    """Single-use verification / reset token (database-schema §3.2, INV-ID-3).

    Plaintext is never stored: only `token_hash` (SHA-256 hex) persists; the
    plaintext lives solely inside the email (security-architecture §5).
    Issuing a new token of a kind supersedes prior unconsumed tokens of that
    kind (consumed_at set) in one transaction (INV-ID-3) — done in the service.
    """

    KIND_EMAIL_VERIFICATION = "email_verification"
    KIND_PASSWORD_RESET = "password_reset"
    KIND_VALUES: ClassVar[tuple[str, str]] = ("email_verification", "password_reset")
    KIND_CHOICES: ClassVar[list[tuple[str, str]]] = [
        ("email_verification", "Email verification"),
        ("password_reset", "Password reset"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)  # UUIDv7 (C-3)
    user = models.ForeignKey(
        "identity.User", on_delete=models.CASCADE, related_name="tokens", db_column="user_id"
    )
    kind = models.TextField(choices=KIND_CHOICES)
    token_hash = models.TextField(unique=True)  # SHA-256 hex; plaintext never stored
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)  # INV-ID-3: single-use
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "user_tokens"  # C-2: fixed by database-schema §3.2
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(kind__in=("email_verification", "password_reset")),
                name="user_tokens_kind_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["user", "kind"], name="user_tokens_user_kind_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"

    def is_live(self, *, now: Any | None = None) -> bool:
        """Token is consumable: unconsumed and not past its TTL (INV-ID-3)."""
        now = now or timezone.now()
        return self.consumed_at is None and self.expires_at > now
