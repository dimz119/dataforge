"""Data-plane API-key authentication (security §3.2; ADR-0011).

The DRF authentication class for the ``X-API-Key`` header. It:

* rejects requests presenting **both** ``Authorization`` and ``X-API-Key`` →
  400 ``ambiguous-credentials`` (A-2);
* parses + env-token-checks the key (SEC-KEY-2), consults the Redis revocation
  cache, constant-time hash-compares, then checks derived state — every failure
  is the single 401 ``invalid-api-key`` (A-3, no state oracle);
* sets ``request.workspace_id`` and ``request.api_key_scopes`` so Layer 1's
  middleware arms the workspace context and Layer 3's ``HasKeyScope`` can gate.

The authenticated principal returned is an ``ApiKeyPrincipal`` — a non-User,
non-AnonymousUser object that reports ``is_authenticated = True`` but carries no
JWT identity (machines are not users). JWT-only surfaces never list this class,
so a key there is an absent credential (SEC-AUTH-1).
"""

from __future__ import annotations

import uuid
from typing import Any

from rest_framework.authentication import BaseAuthentication
from rest_framework.request import Request

from config.problems import AmbiguousCredentials

API_KEY_HEADER = "X-API-Key"


class ApiKeyPrincipal:
    """The request principal for an API-key-authenticated request.

    Not a ``User``: machines hold keys, humans hold JWTs. ``is_authenticated`` is
    ``True`` so DRF's ``IsAuthenticated`` passes; ``is_verified`` is irrelevant
    (key creation already required a verified human).
    """

    is_authenticated = True
    is_anonymous = False

    def __init__(self, *, api_key_id: uuid.UUID, workspace_id: uuid.UUID, scopes: list[str]):
        self.api_key_id = api_key_id
        self.workspace_id = workspace_id
        self.scopes = scopes

    def __str__(self) -> str:
        return f"api_key:{self.api_key_id}"


class ApiKeyAuthentication(BaseAuthentication):
    """``X-API-Key`` authentication for data-plane surfaces (security §3.2)."""

    def authenticate(self, request: Request) -> tuple[ApiKeyPrincipal, None] | None:
        presented = request.headers.get(API_KEY_HEADER)
        has_authorization = bool(request.headers.get("Authorization"))
        if presented is None:
            return None  # no key on this surface → defer to other auth classes
        if has_authorization:
            # Both credential types present → exactly-one-principal rule (A-2).
            raise AmbiguousCredentials()

        # Lazy import: keep the application layer out of DRF's settings-time
        # resolution of DEFAULT_AUTHENTICATION_CLASSES (avoids a circular import).
        from tenancy.application import keys as key_service

        verified = key_service.verify_key(presented)  # raises 401 invalid-api-key
        principal = ApiKeyPrincipal(
            api_key_id=verified.api_key_id,
            workspace_id=verified.workspace_id,
            scopes=verified.scopes,
        )
        # Hand the resolved workspace + scopes to the middleware / Layer 3.
        request.workspace_id = verified.workspace_id  # type: ignore[attr-defined]
        request.api_key_scopes = verified.scopes  # type: ignore[attr-defined]
        request.api_key_id = verified.api_key_id  # type: ignore[attr-defined]
        return principal, None

    def authenticate_header(self, request: Request) -> str:
        # A-7: key surfaces carry WWW-Authenticate: APIKey realm="dataforge".
        return 'APIKey realm="dataforge"'


try:  # pragma: no cover - schema tooling only
    from drf_spectacular.extensions import OpenApiAuthenticationExtension

    class ApiKeyScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
        """drf-spectacular security scheme for the data-plane API key."""

        target_class = "tenancy.api.authentication.ApiKeyAuthentication"
        name = "apiKeyAuth"

        def get_security_definition(self, auto_schema: Any) -> dict[str, str]:
            return {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}
except ImportError:  # pragma: no cover
    pass
