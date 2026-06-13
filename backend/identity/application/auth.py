"""JWT session lifecycle: login, refresh (rotation + reuse revocation), logout.

Implements security §3.1.2/§3.1.3: 15-min access + rotating 7-day refresh,
BLACKLIST_AFTER_ROTATION, refresh-reuse family revocation (SEC-AUTH-9), and the
revoke-all helpers (SEC-AUTH-10) used by password reset/change and deletion.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth.hashers import make_password
from django.db import transaction
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from config.problems import AuthenticationFailedError, AuthenticationRequired
from identity.application.audit import emit
from identity.domain.models import User
from identity.infra.jwt import issue_token_pair


class RefreshReuseError(Exception):
    """A rotated (blacklisted) refresh token was replayed (SEC-AUTH-9)."""


def authenticate(*, email: str, password: str, request: Any | None = None) -> User:
    """Verify credentials; raise 401 `authentication-failed` on mismatch.

    Unverified users CAN log in (INV-ID-2). Soft-deleted accounts cannot. Login
    success and failure are both audited (domain-model §2.10). Argon2id rehash
    on parameter change happens inside Django's `authenticate`.
    """
    user = django_authenticate(request, username=email, password=password)
    if not isinstance(user, User) or user.deleted_at is not None:
        # Burn a hash so timing does not distinguish unknown-user from bad-password.
        make_password(password)
        emit("identity.auth.login_failed", actor=None, metadata={"email_attempted": email})
        raise AuthenticationFailedError()
    emit("identity.user.logged_in", actor=user)
    return user


def login(*, email: str, password: str, request: Any | None = None) -> tuple[RefreshToken, User]:
    """Authenticate and mint a fresh token pair (security §3.1.3)."""
    user = authenticate(email=email, password=password, request=request)
    return issue_token_pair(user), user


def _detect_reuse(raw_refresh: str) -> None:
    """If the presented token is a blacklisted (rotated) refresh, revoke its family.

    SimpleJWT's `RefreshToken(...)` calls `check_blacklist()` during verify and
    raises before we can react, so reuse detection parses the token **without**
    verification, confirms it is a structurally valid refresh whose `jti` is
    blacklisted, and — only then — treats it as a replay (SEC-AUTH-9): revoke ALL
    outstanding refresh tokens for the user, audit, and raise 401.

    The revocation + audit run in their OWN committed transaction so the family
    stays dead even though the function then raises (a raise inside the caller's
    atomic block would otherwise roll the revocation back).
    """
    try:
        unverified = RefreshToken(raw_refresh, verify=False)  # type: ignore[arg-type]
    except TokenError:
        return  # not even structurally a token → ordinary 401 path handles it
    jti = unverified.payload.get("jti")
    if jti is None or not BlacklistedToken.objects.filter(token__jti=jti).exists():
        return
    user_id = str(unverified.payload.get("sub", ""))
    reuse_user = User.objects.filter(id=user_id).first() if user_id else None
    with transaction.atomic():
        if reuse_user is not None:
            revoke_all_refresh_tokens(reuse_user)  # family revocation (SEC-AUTH-9)
        emit("identity.auth.refresh_reused", actor=reuse_user, metadata={"jti": str(jti)})
    raise AuthenticationRequired(
        "Refresh token reuse detected; the session family was revoked."
    )


def rotate_refresh(raw_refresh: str) -> tuple[RefreshToken, User]:
    """Validate + rotate a refresh token; revoke the family on reuse (SEC-AUTH-9).

    Not wrapped in a single atomic block: reuse detection must commit the family
    revocation before raising. The rotation path (blacklist old + issue new) is
    wrapped in its own atomic block below.
    """
    _detect_reuse(raw_refresh)  # replay of a rotated token → 401 + family revocation

    with transaction.atomic():
        try:
            token = RefreshToken(raw_refresh)  # type: ignore[arg-type]  # verifies sig/exp/blacklist
        except TokenError as exc:
            raise AuthenticationRequired() from exc

        user_id = str(token.payload.get("sub", ""))
        user = (
            User.objects.filter(id=user_id, deleted_at__isnull=True).first()
            if user_id
            else None
        )
        if user is None:
            raise AuthenticationRequired()

        # Rotation: blacklist the presented token (BLACKLIST_AFTER_ROTATION) and
        # mint a new pair. blacklist() is no-op-safe if the outstanding row is gone.
        try:
            token.blacklist()
        except (TokenError, AttributeError):
            pass
        return issue_token_pair(user), user


def logout(raw_refresh: str, *, actor: User | None = None) -> None:
    """Blacklist the presented refresh token's family and end the session (SEC-AUTH-5).

    Under rotation the presented token is the only live one in its family, so
    blacklisting it kills the family. Invalid tokens are tolerated (logout is
    idempotent — the goal state is 'session dead').
    """
    try:
        RefreshToken(raw_refresh).blacklist()  # type: ignore[arg-type]
    except (TokenError, AttributeError):
        pass
    if actor is not None:
        emit("identity.user.logged_out", actor=actor)


def refresh_jti(raw_refresh: str) -> str | None:
    """Return the `jti` of a refresh token, or None if it cannot be parsed.

    Used to identify the current session's family so a password *change* spares
    it (SEC-AUTH-10). Parsed without full verification — only the jti is needed.
    """
    try:
        token = RefreshToken(raw_refresh, verify=False)  # type: ignore[arg-type]
    except TokenError:
        return None
    jti = token.payload.get("jti")
    return str(jti) if jti is not None else None


def revoke_all_refresh_tokens(user: User, *, except_jti: str | None = None) -> int:
    """Blacklist every outstanding refresh token for `user` (SEC-AUTH-10).

    `except_jti` spares one token (the current session on password *change*, so
    the user who just proved their password is not logged out). Returns the count
    blacklisted. API keys are untouched — machine credentials are independent.
    """
    count = 0
    outstanding = OutstandingToken.objects.filter(user=user)
    if except_jti is not None:
        outstanding = outstanding.exclude(jti=except_jti)
    for row in outstanding:
        _, created = BlacklistedToken.objects.get_or_create(token=row)
        if created:
            count += 1
    return count
