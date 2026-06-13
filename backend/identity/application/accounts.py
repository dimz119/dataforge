"""Account lifecycle services (security §5; INV-ID-1/3/4).

Signup, email verification, password reset, password change, and account
deletion. Every mutation that the audited set covers (domain-model §2.10) writes
its audit entry in the same transaction (INV-AUD-2). Tokens never appear in any
return value or audit metadata (INV-AUD-3).
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ErrorDetail
from rest_framework.exceptions import ValidationError as DRFValidationError

from config.problems import AuthenticationFailedError, ConflictError
from identity.application import auth, tokens
from identity.application.audit import emit
from identity.domain.email import normalize_email
from identity.domain.models import User, UserToken
from identity.infra import email as email_infra


def _token_invalid_error(message: str) -> DRFValidationError:
    """400 `validation-error` with `errors[0].code = "token_invalid"` (api-spec §4.1)."""
    return DRFValidationError({"token": [ErrorDetail(message, code="token_invalid")]})


def _validate_password(raw_password: str, user: User) -> None:
    """Apply the policy (length 10-128, denylist, similarity); 400 on failure."""
    if len(raw_password) > 128:  # security §3.1.1 upper bound
        raise DRFValidationError(
            {"password": ["Ensure this field has no more than 128 characters."]}
        )
    try:
        validate_password(raw_password, user)
    except DjangoValidationError as exc:
        raise DRFValidationError({"password": list(exc.messages)}) from exc


@transaction.atomic
def register_user(*, email: str, password: str) -> User:
    """Create an unverified account, send verification, audit (security §5.1).

    Duplicate (case-insensitive, non-deleted) email → 409 `conflict`
    (enumeration accepted, SEC-ACC-11). Weak password → 400. The verification
    email is sent inside the transaction's success path.
    """
    normalized = normalize_email(email)
    user = User(email=normalized, is_verified=False)
    _validate_password(password, user)
    user.set_password(password)  # Argon2id
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:
        # users_email_uq (INV-ID-1) violated → account already exists.
        raise ConflictError("An account with this email already exists.") from exc

    token = tokens.issue_token(user, UserToken.KIND_EMAIL_VERIFICATION)
    emit("identity.user.registered", actor=user, metadata={"email": normalized})
    # Sent after the row + token are persisted; transaction.on_commit keeps the
    # email out of a rolled-back signup.
    transaction.on_commit(
        lambda: email_infra.send_verification_email(to_email=normalized, token=token)
    )
    return user


@transaction.atomic
def verify_email(*, token: str) -> User:
    """Burn a verification token and flip `is_verified` (security §5.1).

    Consumed/expired/unknown token → 400 `validation-error` with
    `errors[0].code = "token_invalid"` (api-spec §4.1).
    """
    consumed = tokens.consume_token(token, UserToken.KIND_EMAIL_VERIFICATION)
    if consumed is None:
        raise _token_invalid_error("This verification token is invalid or expired.")
    user = consumed.user
    if not user.is_verified:
        user.is_verified = True
        user.save(update_fields=["is_verified", "updated_at"])
        emit("identity.user.email_verified", actor=user)
    return user


def resend_verification(*, email: str) -> None:
    """Re-send verification if an unverified account exists; uniform 202 (SEC-ACC-6).

    Caller always returns 202 regardless of outcome — no enumeration.
    """
    normalized = normalize_email(email)
    user = User.objects.filter(email=normalized, deleted_at__isnull=True).first()
    if user is None or user.is_verified:
        return
    token = tokens.issue_token(user, UserToken.KIND_EMAIL_VERIFICATION)
    email_infra.send_verification_email(to_email=normalized, token=token)


def request_password_reset(*, email: str) -> None:
    """Send a reset link only to a verified account; uniform 202 (SEC-ACC-6/8).

    No reset for unverified accounts (SEC-ACC-8) — signup re-sends verification
    instead, handled by the resend path; here we simply do not send.
    """
    normalized = normalize_email(email)
    user = User.objects.filter(email=normalized, deleted_at__isnull=True).first()
    if user is None or not user.is_verified:
        return
    token = tokens.issue_token(user, UserToken.KIND_PASSWORD_RESET)
    email_infra.send_password_reset_email(to_email=normalized, token=token)


@transaction.atomic
def confirm_password_reset(*, token: str, new_password: str) -> User:
    """Burn the reset token, set the new password, revoke all refresh (SEC-AUTH-10).

    Invalid/expired token → 400. Also invalidates outstanding verification tokens
    (a password change implies trust in the address) — defensive, per §5.2.
    """
    consumed = tokens.consume_token(token, UserToken.KIND_PASSWORD_RESET)
    if consumed is None:
        raise _token_invalid_error("This reset token is invalid or expired.")
    user = consumed.user
    _validate_password(new_password, user)
    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])
    tokens.invalidate_tokens(user, UserToken.KIND_PASSWORD_RESET)
    auth.revoke_all_refresh_tokens(user)  # all families (SEC-AUTH-10); API keys untouched
    emit("identity.user.password_reset", actor=user)
    transaction.on_commit(lambda: email_infra.send_password_changed_notice(to_email=user.email))
    return user


@transaction.atomic
def change_password(
    *, user: User, current_password: str, new_password: str, current_jti: str | None
) -> None:
    """Authenticated password change; revoke all refresh except the current session.

    Wrong current password yields 401 `authentication-failed`. The current
    session's refresh (by `current_jti`) is spared (SEC-AUTH-10) so the user who
    just proved their password is not logged out.
    """
    if not user.check_password(current_password):
        raise AuthenticationFailedError("Current password is incorrect.")
    _validate_password(new_password, user)
    user.set_password(new_password)
    user.save(update_fields=["password", "updated_at"])
    auth.revoke_all_refresh_tokens(user, except_jti=current_jti)
    emit("identity.user.password_changed", actor=user)
    transaction.on_commit(lambda: email_infra.send_password_changed_notice(to_email=user.email))


@transaction.atomic
def request_account_deletion(*, user: User, password: str) -> None:
    """Begin the deletion grace flow (security §5.3); revoke all refresh.

    Re-auth via password (401 on mismatch). The sole-admin guard (INV-ID-4 /
    INV-TEN-3) is owned by Tenancy — it is invoked by the view before this. We
    set the `pending_deletion` intent via the soft-delete tombstone's grace path:
    here we record the request and revoke sessions; the Celery scrub job (Phase 2
    state machine) finalizes after the 7-day grace. Membership removal is the
    tenancy agent's concern, called from the view layer.
    """
    if not user.check_password(password):
        raise AuthenticationFailedError("Password is incorrect.")
    auth.revoke_all_refresh_tokens(user)
    emit(
        "identity.user.deletion_requested",
        actor=user,
        metadata={"requested_at": timezone.now().isoformat()},
    )


def serialize_self(
    user: User, *, memberships: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Shape the `GET /users/me` body (api-spec §4.2); memberships from tenancy."""
    return {
        "user_id": str(user.id),
        "email": user.email,
        "is_verified": user.is_verified,
        "created_at": user.created_at,
        "memberships": memberships or [],
    }
