"""Request-correlation middleware (observability §3.1).

Mints a UUIDv7 `request_id` per inbound request, binds it (plus W3C trace
context) into structlog contextvars, and echoes it as the `X-Request-Id`
response header.
"""

import re
import secrets
from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse

from observation.domain.ids import uuid7

_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-[0-9a-f]{2}$"
)


def _trace_context(request: HttpRequest) -> tuple[str, str]:
    """Accept an inbound W3C `traceparent`, or generate fresh ids (observability §3.1)."""
    header = request.headers.get("traceparent", "")
    match = _TRACEPARENT_RE.match(header.strip().lower())
    if match:
        return match.group("trace_id"), secrets.token_hex(8)
    return secrets.token_hex(16), secrets.token_hex(8)


class RequestIdMiddleware:
    """X-Request-Id (UUIDv7) → structlog context (backend-architecture §5.1)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = str(uuid7())
        trace_id, span_id = _trace_context(request)
        request.request_id = request_id  # type: ignore[attr-defined]
        structlog.contextvars.bind_contextvars(
            request_id=request_id, trace_id=trace_id, span_id=span_id
        )
        try:
            response = self.get_response(request)
            response["X-Request-Id"] = request_id
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "trace_id", "span_id")
