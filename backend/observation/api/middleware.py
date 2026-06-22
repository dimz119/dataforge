"""Request-correlation middleware (observability §3.1).

Mints a UUIDv7 `request_id` per inbound request, binds it (plus W3C trace
context) into structlog contextvars, and echoes it as the `X-Request-Id`
response header.
"""

import re
import secrets
import time
from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse

from observation.domain.ids import uuid7

_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-[0-9a-f]{2}$"
)

# Routes excluded from df_http_* (high-frequency, no SLO signal): the metrics
# scrape itself + the health probes. They still receive an X-Request-Id header.
_UNINSTRUMENTED_ROUTES = frozenset({"metrics", "healthz", "readyz"})


def _route_label(request: HttpRequest) -> str:
    """The bounded URL-pattern label for df_http_* (M-3: never the raw path).

    ``resolver_match.route`` is the registered URLconf pattern (e.g.
    ``api/v1/streams/<uuid:stream_id>/events``) — bounded by the number of routes,
    so it is an admissible label. An unresolved path (a 404 before routing, or an
    unknown URL) collapses to ``<unmatched>`` to keep cardinality bounded (M-3).
    """
    match = getattr(request, "resolver_match", None)
    if match is not None and getattr(match, "route", None):
        return str(match.route)
    return "<unmatched>"


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
        from observation.infra import metrics

        request_id = str(uuid7())
        trace_id, span_id = _trace_context(request)
        request.request_id = request_id  # type: ignore[attr-defined]
        structlog.contextvars.bind_contextvars(
            request_id=request_id, trace_id=trace_id, span_id=span_id
        )
        # df_http_requests_in_flight (§4 web family): concurrency gauge over the
        # whole request lifetime, inc/dec symmetric even on an exception.
        metrics.http_requests_in_flight.inc()
        started = time.perf_counter()
        try:
            response = self.get_response(request)
            response["X-Request-Id"] = request_id
            self._record(request, response.status_code, started, metrics)
            return response
        except Exception:
            # An unhandled view exception becomes a 500 downstream; record it as such
            # so df_http_requests_total counts it toward SLO-1 (status≥500 = bad).
            self._record(request, 500, started, metrics)
            raise
        finally:
            metrics.http_requests_in_flight.dec()
            structlog.contextvars.unbind_contextvars("request_id", "trace_id", "span_id")

    @staticmethod
    def _record(
        request: HttpRequest, status: int, started: float, metrics: object
    ) -> None:
        """Emit df_http_requests_total{method,route,status} + the duration histogram.

        The route is the bounded URL pattern (M-3); the metrics scrape + health
        probes are skipped (no SLO signal). SLO-1 reads these series: bad = status
        ≥ 500; 4xx are GOOD (a well-formed rejection, observability §7).
        """
        route = _route_label(request)
        if route in _UNINSTRUMENTED_ROUTES:
            return
        method = request.method or "UNKNOWN"
        elapsed = time.perf_counter() - started
        metrics.http_requests_total.labels(  # type: ignore[attr-defined]
            method=method, route=route, status=str(status)
        ).inc()
        metrics.http_request_duration_seconds.labels(  # type: ignore[attr-defined]
            method=method, route=route
        ).observe(elapsed)
