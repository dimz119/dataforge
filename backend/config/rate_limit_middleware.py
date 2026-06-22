"""Per-key rate-limit middleware (P11-08; api-spec §2.8; scaling §5).

Four Redis token buckets, keyed per API key, by request *scope*:

| scope      | limit (api-spec §2.8) | routes                                       |
|------------|-----------------------|----------------------------------------------|
| data-events| 600 / min             | the data-plane event read surface (`/events`)|
| lifecycle  | 30 / min              | stream start/stop/pause/resume verbs         |
| control    | 120 / min             | every other authenticated `/api/v1/*` route  |
| ws-connect | 10 / min              | WS upgrade handshakes (enforced in ASGI)     |

A request over its bucket is refused with ``429 rate-limited`` (RFC 9457
``config.problems.RateLimited``) carrying ``Retry-After`` and bumps
``df_rate_limited_total{scope}`` (M-3: the only label is the bounded ``scope``).

Keying without the DB: DRF authentication runs *inside the view* (after this
middleware), so ``request.api_key_id`` is not yet set here. The limiter keys on the
key's **public prefix** (``df_<env>_<prefix>``), parsed from the raw ``X-API-Key``
header — the non-secret durable handle (never the secret/hash; INV-AUD-3). A request
with no ``X-API-Key`` (a JWT/console request, or an anonymous probe) is keyed by the
trusted-edge client IP under the ``control`` scope, so console traffic is bounded too
without coupling to JWT decode. The limiter fails **open** on a degraded cache.

The WS connect bucket (10/min) is NOT enforced here — WS upgrades arrive on the ASGI
path, not the WSGI middleware chain. :func:`ws_connect_allowed` exposes the same
bucket for the WS consumer's connect handler (delivery layer) to call.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from config.problems import RateLimited
from identity.infra import rate_limit
from identity.infra.rate_limit import TokenBucket
from tenancy.api.authentication import API_KEY_HEADER
from tenancy.infra.keys import parse_key

logger = structlog.get_logger(__name__)

# Per-minute token buckets (api-spec §2.8). capacity == per-minute limit; the refill
# rate is limit/60 tokens per second (smooth, no window-edge burst doubling).
SCOPE_DATA_EVENTS = "data-events"
SCOPE_CONTROL = "control"
SCOPE_LIFECYCLE = "lifecycle"
SCOPE_WS_CONNECT = "ws-connect"

_BUCKETS: dict[str, TokenBucket] = {
    SCOPE_DATA_EVENTS: TokenBucket(capacity=600, rate_per_sec=600 / 60),
    SCOPE_CONTROL: TokenBucket(capacity=120, rate_per_sec=120 / 60),
    SCOPE_LIFECYCLE: TokenBucket(capacity=30, rate_per_sec=30 / 60),
    SCOPE_WS_CONNECT: TokenBucket(capacity=10, rate_per_sec=10 / 60),
}

# The route names (URLconf ``name=``) that count as lifecycle commands (30/min).
_LIFECYCLE_ROUTE_NAMES = frozenset(
    {"stream-start", "stream-stop", "stream-pause", "stream-resume"}
)
# The route names that serve data-plane events (600/min).
_DATA_EVENT_ROUTE_NAMES = frozenset({"stream-events"})

# Paths exempt from rate limiting: the platform probes + the metrics scrape target
# (operability must never be rate-limited; observability §4 / backend-arch §6).
_EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def _scope_for(request: HttpRequest) -> str:
    """Classify a request into a rate-limit scope by its resolved route name.

    Falls back to ``control`` for any authenticated route without a more specific
    classification (the api-spec §2.8 default control-plane bucket).
    """
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match is not None else None
    if name in _DATA_EVENT_ROUTE_NAMES:
        return SCOPE_DATA_EVENTS
    if name in _LIFECYCLE_ROUTE_NAMES:
        return SCOPE_LIFECYCLE
    return SCOPE_CONTROL


def _identifier(request: HttpRequest) -> str:
    """The rate-limit subject: the key's public prefix, else the trusted-edge IP.

    The ``X-API-Key`` prefix (``df_<env>_<prefix>``) is the durable, non-secret
    handle — parsing only reads the public segment, never the secret. JWT/anonymous
    requests have no key, so they are keyed by the trusted client IP (the same
    ``Fly-Client-IP``-first resolution the signup limiter uses; never the
    client-controlled ``X-Forwarded-For``).
    """
    presented = request.headers.get(API_KEY_HEADER)
    if presented:
        parsed = parse_key(presented)
        if parsed is not None:
            return f"key:{parsed.key_prefix}"
    return f"ip:{rate_limit.client_ip(request)}"


class RateLimitMiddleware:
    """Per-key token-bucket rate limiting on the WSGI request path (P11-08).

    Ordered AFTER ``WorkspaceContextMiddleware`` so ``resolver_match`` is populated
    (the scope classifier reads the resolved route name). Disabled when
    ``DF_RATE_LIMIT_ENABLED`` is false (the test default, so existing suites are not
    throttled); production enables it.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.enabled = bool(getattr(settings, "DF_RATE_LIMIT_ENABLED", False))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self.enabled or request.path in _EXEMPT_PATHS:
            return self.get_response(request)
        scope = _scope_for(request)
        bucket = _BUCKETS[scope]
        result = rate_limit.check_token_bucket(scope, _identifier(request), bucket)
        if not result.allowed:
            return _rate_limited_response(scope, result.retry_after)
        return self.get_response(request)


def _rate_limited_response(scope: str, retry_after: int) -> HttpResponse:
    """The RFC 9457 ``rate-limited`` problem + ``Retry-After`` + metric (P11-08).

    Built directly (not raised) because middleware runs outside DRF's exception
    handler. Mirrors the body ``config.problems.RateLimited`` produces so the
    contract type/slug/extension are uniform with the DRF surfaces.
    """
    from observation.infra import metrics

    metrics.rate_limited_total.labels(scope=scope).inc()
    problem = RateLimited(retry_after=retry_after)
    body = {
        "type": f"{_problem_base()}/rate-limited",
        "title": "Too Many Requests",
        "status": 429,
        "detail": str(problem.detail),
        "retry_after_seconds": retry_after,
        "scope": scope,
    }
    response = JsonResponse(body, status=429, content_type="application/problem+json")
    response["Retry-After"] = str(retry_after)
    return response


def _problem_base() -> str:
    from config.problems import PROBLEM_BASE

    return PROBLEM_BASE


def ws_connect_allowed(identifier: str) -> rate_limit.RateLimitResult:
    """Consume one WS-connect token for ``identifier`` (P11-08; 10/min per key).

    Exposed for the delivery layer's WS connect handler (the ASGI path bypasses this
    WSGI middleware). ``identifier`` is the key prefix (``key:df_…``) or client IP,
    keyed exactly as :func:`_identifier` does, so the WS and REST surfaces share one
    keying convention. On denial the consumer closes the upgrade with the WS policy
    code and bumps ``df_ws_connect_total{result="quota_rejected"}`` (delivery owns
    that metric); this returns the decision + Retry-After only.
    """
    return rate_limit.check_token_bucket(
        SCOPE_WS_CONNECT, identifier, _BUCKETS[SCOPE_WS_CONNECT]
    )
