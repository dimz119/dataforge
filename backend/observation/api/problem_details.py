"""RFC 9457 problem-details exception handler (backend-architecture §6).

One EXCEPTION_HANDLER renders every DRF error as `application/problem+json`
with `type`, `title`, `status`, `detail`, `instance` and `request_id`. The
problem-type registry is owned by api-specification §2.7; DRF ValidationError
maps to `…/problems/validation-error` with an `errors[]` extension carrying
field pointers.

The closed slug catalog and the 401/403/404 policy exceptions live in
`config.problems` (identity-owned, reused by tenancy/keys). Raising a
`config.problems.ProblemException` carries its own `slug`, extension members,
and response headers (e.g. `WWW-Authenticate`, `Retry-After`) through here.
"""

from typing import Any

from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from config.problems import PROBLEM_BASE, ProblemException

# Stock DRF exceptions → closed catalog slugs (api-spec §2.7.1; security §3.3).
# Note: DRF's NotAuthenticated and AuthenticationFailed both map to
# `authentication-required` (the §3.3 row-1 default for JWT surfaces). The
# distinct `authentication-failed` (bad login) and `invalid-api-key` slugs are
# raised explicitly via config.problems, never inferred from a stock type.
_TYPE_SLUGS: dict[type[Exception], str] = {
    exceptions.ValidationError: "validation-error",
    exceptions.ParseError: "validation-error",
    exceptions.NotAuthenticated: "authentication-required",
    exceptions.AuthenticationFailed: "authentication-required",
    exceptions.PermissionDenied: "permission-denied",
    exceptions.NotFound: "not-found",
    exceptions.Throttled: "rate-limited",
}


def _validation_errors(detail: Any, prefix: str = "") -> list[dict[str, str]]:
    """Flatten DRF ValidationError detail into `errors[] {field, code, message}`."""
    errors: list[dict[str, str]] = []
    if isinstance(detail, dict):
        for field, value in detail.items():
            path = f"{prefix}.{field}" if prefix else str(field)
            errors.extend(_validation_errors(value, path))
    elif isinstance(detail, list):
        for item in detail:
            errors.extend(_validation_errors(item, prefix))
    else:
        errors.append(
            {
                "field": prefix or "non_field_errors",
                "code": str(getattr(detail, "code", "invalid")),
                "message": str(detail),
            }
        )
    return errors


def problem_details_exception_handler(
    exc: Exception, context: dict[str, Any]
) -> Response | None:
    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # non-API exception → Django's 500 path

    request = context.get("request")
    # config.problems exceptions carry their own closed-catalog slug; stock DRF
    # exceptions map by type.
    slug = exc.slug if isinstance(exc, ProblemException) else _TYPE_SLUGS.get(type(exc))
    problem: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}/{slug}" if slug else "about:blank",
        "title": getattr(exc, "default_detail", "Request failed").__str__(),
        "status": response.status_code,
        "detail": str(getattr(exc, "detail", exc)),
        "instance": getattr(request, "path", ""),
    }
    request_id = getattr(request, "request_id", None)
    if request_id is not None:
        problem["request_id"] = f"req_{request_id}"

    if isinstance(exc, exceptions.ValidationError):
        problem["title"] = "Request validation failed"
        problem["detail"] = "Request validation failed."
        problem["errors"] = _validation_errors(exc.detail)

    # Extension members (required_scope, required_role, retry_after_seconds, …)
    # and bespoke response headers (WWW-Authenticate, Retry-After) per §3.3 / A-7.
    if isinstance(exc, ProblemException):
        problem.update(exc.extensions)
        for header, value in exc.headers.items():
            response[header] = value

    response.data = problem
    response.content_type = "application/problem+json"
    return response
