"""SimpleJWT integration for the Identity context (security-architecture §3.1.2).

Owns the access/refresh token mint (with the frozen claim set incl.
`is_verified`, no workspace claim) and the DRF authentication class. Other apps
(tenancy, keys) reuse `DataForgeJWTAuthentication` indirectly via the DRF default
authentication classes; the closed problem slugs are emitted by the shared
RFC 9457 handler (config.problems / observation.api.problem_details).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import AbstractBaseUser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework_simplejwt.tokens import Token

    from identity.domain.models import User


def _record_jwt_auth_failure(reason: str) -> None:
    """df_auth_failures_total{mechanism=jwt,reason} (observability §4; AuthFailureSpike).

    ``reason`` is a closed slug (never the token, M-3 / redaction). Best-effort — a
    metrics failure must never turn an auth rejection into a 500.
    """
    try:
        from observation.infra import metrics

        metrics.auth_failures_total.labels(mechanism="jwt", reason=reason).inc()
    except Exception:  # pragma: no cover - metrics must never break auth
        pass


def issue_token_pair(user: User) -> RefreshToken:
    """Mint a refresh token whose access copy carries the frozen claim set.

    Access claims: `sub` (user id, USER_ID_CLAIM), `jti`, `iat`, `exp`,
    `token_type`, and `is_verified`. **No workspace claim** — membership is
    resolved per request (security §3.1.2), so role/membership changes take
    effect immediately rather than at token expiry.
    """
    refresh = RefreshToken.for_user(user)
    # Stamp the verification gate into the token so DRF permissions can read it
    # without a DB round-trip; the source of truth remains the row (re-checked
    # for tenant-creating commands, INV-ID-2).
    refresh["is_verified"] = bool(user.is_verified)
    refresh.access_token["is_verified"] = bool(user.is_verified)
    return refresh


class DataForgeJWTAuthentication(JWTAuthentication):
    """Console JWT auth.

    Resolves the access token to the live `User` row and rejects tombstoned
    accounts (INV-ID-4). A missing/malformed/expired token surfaces as the
    shared handler's `authentication-required` 401 (security §3.3 row 1);
    SimpleJWT's `InvalidToken`/`AuthenticationFailed` both map there.
    """

    def authenticate(self, request: Request) -> tuple[AbstractBaseUser, Token] | None:  # type: ignore[override]
        """Authenticate, recording df_auth_failures_total{mechanism=jwt} on rejection.

        A missing/absent Authorization header returns ``None`` (defer, not a failure);
        an invalid/expired/tombstoned token raises — counted once as a jwt failure
        with the closed reason slug (never the token value, M-3).
        """
        try:
            return super().authenticate(request)
        except AuthenticationFailed as exc:
            _record_jwt_auth_failure(getattr(exc, "default_code", None) or "invalid_token")
            raise

    def get_user(self, validated_token: Token) -> AbstractBaseUser:  # type: ignore[override]
        user: AbstractBaseUser = super().get_user(validated_token)
        # Soft-deleted accounts hold no live session (INV-ID-4); treat the token
        # as invalid rather than authenticating a tombstone.
        if getattr(user, "deleted_at", None) is not None:
            raise AuthenticationFailed("Account is no longer active.")
        return user


try:  # pragma: no cover - schema tooling only
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class DataForgeJWTScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
        """drf-spectacular security scheme for the console JWT (--fail-on-warn clean)."""

        target_class = "identity.infra.jwt.DataForgeJWTAuthentication"
        name = "jwtAuth"

        def get_security_definition(self, auto_schema: Any) -> dict[str, str]:
            return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
except ImportError:  # pragma: no cover
    pass
