"""Shared RFC 9457 problem catalog and the 401/403/404 cross-tenant policy.

The single source of truth for every auth / cross-tenant response. Problem-type
slugs come from the closed catalog (api-specification §2.7.1); the status mapping
and the situation→slug rules are fixed by security-architecture §3.3.

Identity owns this module; **tenancy and api-keys import these exceptions** so
the whole platform emits one uniform set of slugs. Raising one of these from any
view (or DRF authentication / permission class) yields the correct
`application/problem+json` body via observation.api.problem_details.

Policy (security §3.3), implemented by the exceptions below:

* missing/malformed/expired JWT on a JWT surface → 401 `authentication-required`
* bad email/password at login                    → 401 `authentication-failed`
* any API-key failure (unknown/revoked/expired/wrong-env) → 401 `invalid-api-key`
* both Authorization and X-API-Key present        → 400 `ambiguous-credentials`
* valid credential, insufficient scope/role in own workspace → 403
  `permission-denied` (+ required_scope / required_role)
* valid credential, foreign-workspace OR nonexistent target → 404 `not-found`
  (foreign and absent are indistinguishable — never `permission-denied`)
* unverified user on a tenant-creating command (INV-ID-2) → 403 `email-not-verified`
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from rest_framework import status
from rest_framework.exceptions import APIException

PROBLEM_BASE: Final = "https://docs.dataforge.dev/problems"


# --- Closed slug catalog (api-specification §2.7.1) --------------------------
class Slug:
    """The closed MVP problem-type slug set (api-spec §2.7.1)."""

    VALIDATION_ERROR: Final = "validation-error"
    CURSOR_INVALID: Final = "cursor-invalid"
    AMBIGUOUS_CREDENTIALS: Final = "ambiguous-credentials"
    AUTHENTICATION_REQUIRED: Final = "authentication-required"
    AUTHENTICATION_FAILED: Final = "authentication-failed"
    INVALID_API_KEY: Final = "invalid-api-key"
    EMAIL_NOT_VERIFIED: Final = "email-not-verified"
    PERMISSION_DENIED: Final = "permission-denied"
    QUOTA_EXCEEDED: Final = "quota-exceeded"
    NOT_FOUND: Final = "not-found"
    CONFLICT: Final = "conflict"
    INVALID_STATE_TRANSITION: Final = "invalid-state-transition"
    IDEMPOTENCY_KEY_CONFLICT: Final = "idempotency-key-conflict"
    CURSOR_EXPIRED: Final = "cursor-expired"
    PAYLOAD_TOO_LARGE: Final = "payload-too-large"
    MANIFEST_VALIDATION_FAILED: Final = "manifest-validation-failed"
    RATE_LIMITED: Final = "rate-limited"
    INTERNAL_ERROR: Final = "internal-error"
    SERVICE_UNAVAILABLE: Final = "service-unavailable"


class ProblemException(APIException):
    """Base class for DataForge problems with an explicit closed-catalog slug.

    The RFC 9457 handler reads `slug` to build `type`, and `extensions` to add
    contract extension members (e.g. `required_scope`, `required_role`).
    """

    slug: ClassVar[str] = Slug.INTERNAL_ERROR

    def __init__(
        self,
        detail: str | None = None,
        *,
        extensions: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.extensions: dict[str, Any] = extensions or {}
        self.headers: dict[str, str] = headers or {}


# --- 401 -------------------------------------------------------------------
class AuthenticationRequired(ProblemException):
    """401 — missing/malformed/expired JWT on a JWT surface (security §3.3)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Authentication credentials were not provided or are invalid."
    slug = Slug.AUTHENTICATION_REQUIRED

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        # A-7: JWT surfaces carry WWW-Authenticate: Bearer.
        headers = {"WWW-Authenticate": "Bearer"}
        headers.update(kwargs.pop("headers", {}) or {})
        super().__init__(detail, headers=headers, **kwargs)


class AuthenticationFailedError(ProblemException):
    """401 — bad email/password at login; distinct slug for the login form."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Incorrect email or password."
    slug = Slug.AUTHENTICATION_FAILED

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        headers = {"WWW-Authenticate": "Bearer"}
        headers.update(kwargs.pop("headers", {}) or {})
        super().__init__(detail, headers=headers, **kwargs)


class InvalidApiKey(ProblemException):
    """401 — every API-key failure: unknown/revoked/expired/wrong-env (A-3)."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "The presented API key is unknown, revoked, or expired."
    slug = Slug.INVALID_API_KEY

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        # A-7: key surfaces carry WWW-Authenticate: APIKey realm="dataforge".
        headers = {"WWW-Authenticate": 'APIKey realm="dataforge"'}
        headers.update(kwargs.pop("headers", {}) or {})
        super().__init__(detail, headers=headers, **kwargs)


# --- 400 -------------------------------------------------------------------
class AmbiguousCredentials(ProblemException):
    """400 — both Authorization and X-API-Key present (A-2)."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Provide exactly one of Authorization or X-API-Key, not both."
    slug = Slug.AMBIGUOUS_CREDENTIALS


# --- 403 -------------------------------------------------------------------
class EmailNotVerified(ProblemException):
    """403 — tenant-creating command by an unverified user (INV-ID-2, A-6)."""

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Verify your email address before performing this action."
    slug = Slug.EMAIL_NOT_VERIFIED


class PermissionDeniedError(ProblemException):
    """403 — valid credential, insufficient scope/role within own workspace.

    Names the missing privilege via `required_scope` (key) or `required_role`
    (JWT) so it is fixable (security §3.3). Never used for foreign-workspace
    access — that is masked to 404 (`NotFoundError`).
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "You do not have permission to perform this action."
    slug = Slug.PERMISSION_DENIED

    def __init__(
        self,
        detail: str | None = None,
        *,
        required_scope: str | None = None,
        required_role: str | None = None,
        **kwargs: Any,
    ) -> None:
        extensions = dict(kwargs.pop("extensions", {}) or {})
        if required_scope is not None:
            extensions["required_scope"] = required_scope
        if required_role is not None:
            extensions["required_role"] = required_role
        super().__init__(detail, extensions=extensions, **kwargs)


# --- 404 -------------------------------------------------------------------
class NotFoundError(ProblemException):
    """404 — absent resource OR cross-tenant masking (W-3, security §3.3).

    Foreign-workspace and nonexistent are indistinguishable by status code:
    existence of foreign resources is never confirmed.
    """

    status_code = status.HTTP_404_NOT_FOUND
    default_detail = "The requested resource was not found."
    slug = Slug.NOT_FOUND


# --- 409 -------------------------------------------------------------------
class ConflictError(ProblemException):
    """409 — uniqueness/state conflicts (duplicate email/slug, sole-admin)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state."
    slug = Slug.CONFLICT


# --- 429 -------------------------------------------------------------------
class RateLimited(ProblemException):
    """429 — rate-limit bucket exhausted (§2.8); carries Retry-After."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Too many requests; slow down."
    slug = Slug.RATE_LIMITED

    def __init__(self, detail: str | None = None, *, retry_after: int = 60, **kwargs: Any) -> None:
        headers = {"Retry-After": str(retry_after)}
        headers.update(kwargs.pop("headers", {}) or {})
        extensions = dict(kwargs.pop("extensions", {}) or {})
        extensions["retry_after_seconds"] = retry_after
        super().__init__(detail, headers=headers, extensions=extensions, **kwargs)


__all__ = [
    "PROBLEM_BASE",
    "AmbiguousCredentials",
    "AuthenticationFailedError",
    "AuthenticationRequired",
    "ConflictError",
    "EmailNotVerified",
    "InvalidApiKey",
    "NotFoundError",
    "PermissionDeniedError",
    "ProblemException",
    "RateLimited",
    "Slug",
]
