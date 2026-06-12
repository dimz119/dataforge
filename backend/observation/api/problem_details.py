"""RFC 9457 problem-details exception handler (backend-architecture §6).

One EXCEPTION_HANDLER renders every DRF error as `application/problem+json`
with `type`, `title`, `status`, `detail`, `instance` and `request_id`. The
problem-type registry is owned by api-specification §2.7; DRF ValidationError
maps to `…/problems/validation-error` with an `errors[]` extension carrying
field pointers.
"""

from typing import Any

from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

PROBLEM_BASE = "https://docs.dataforge.dev/problems"

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
    slug = _TYPE_SLUGS.get(type(exc))
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

    response.data = problem
    response.content_type = "application/problem+json"
    return response
