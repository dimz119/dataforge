"""The shared 401/403/404 problem helpers (config.problems) and the handler.

These slugs are reused by tenancy/keys, so the closed catalog and the
status/slug/extension mapping are asserted here (security §3.3, api-spec §2.7.1).
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.test import APIRequestFactory

from config import problems
from observation.api.problem_details import problem_details_exception_handler


def _render(exc: Exception) -> tuple[dict[str, Any], Any]:
    request = APIRequestFactory().get("/api/v1/anything")
    response = problem_details_exception_handler(exc, {"request": request})
    assert response is not None
    return dict(response.data), response


def test_authentication_required_maps_401_and_www_authenticate() -> None:
    data, response = _render(problems.AuthenticationRequired())
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert data["type"].endswith("/authentication-required")
    assert data["status"] == 401
    assert response["WWW-Authenticate"] == "Bearer"


def test_authentication_failed_distinct_slug() -> None:
    data, response = _render(problems.AuthenticationFailedError())
    assert response.status_code == 401
    assert data["type"].endswith("/authentication-failed")


def test_invalid_api_key_slug_and_realm_header() -> None:
    data, response = _render(problems.InvalidApiKey())
    assert response.status_code == 401
    assert data["type"].endswith("/invalid-api-key")
    assert response["WWW-Authenticate"] == 'APIKey realm="dataforge"'


def test_ambiguous_credentials_is_400() -> None:
    data, response = _render(problems.AmbiguousCredentials())
    assert response.status_code == 400
    assert data["type"].endswith("/ambiguous-credentials")


def test_email_not_verified_is_403() -> None:
    data, response = _render(problems.EmailNotVerified())
    assert response.status_code == 403
    assert data["type"].endswith("/email-not-verified")


def test_permission_denied_carries_required_scope() -> None:
    data, response = _render(problems.PermissionDeniedError(required_scope="streams:write"))
    assert response.status_code == 403
    assert data["type"].endswith("/permission-denied")
    assert data["required_scope"] == "streams:write"


def test_permission_denied_carries_required_role() -> None:
    data, _ = _render(problems.PermissionDeniedError(required_role="admin"))
    assert data["required_role"] == "admin"


def test_not_found_masks_foreign_and_absent() -> None:
    data, response = _render(problems.NotFoundError())
    assert response.status_code == 404
    assert data["type"].endswith("/not-found")


def test_conflict_is_409() -> None:
    data, response = _render(problems.ConflictError("Duplicate."))
    assert response.status_code == 409
    assert data["type"].endswith("/conflict")


def test_rate_limited_carries_retry_after() -> None:
    data, response = _render(problems.RateLimited(retry_after=42))
    assert response.status_code == 429
    assert data["type"].endswith("/rate-limited")
    assert data["retry_after_seconds"] == 42
    assert response["Retry-After"] == "42"


def test_problem_body_is_problem_json_content_type() -> None:
    _, response = _render(problems.NotFoundError())
    assert response.content_type == "application/problem+json"
