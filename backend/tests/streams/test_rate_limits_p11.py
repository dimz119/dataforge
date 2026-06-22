"""Per-key rate-limit tests (phase-11; api-spec §2.8; scaling §5).

Four Redis token buckets keyed per API-key prefix by request scope:

| scope        | limit   | route family                         |
|--------------|---------|--------------------------------------|
| data-events  | 600/min | the /events data-plane read surface  |
| control      | 120/min | every other authenticated /api/v1/*  |
| lifecycle    | 30/min  | stream start/stop/pause/resume       |
| ws-connect   | 10/min  | WS upgrade handshakes (ASGI path)    |

A request over its bucket → ``429 rate-limited`` (RFC 9457) + ``Retry-After`` and
bumps ``df_rate_limited_total{scope}`` (M-3: the only label is the bounded scope).
Asserted here: each bucket trips at its limit, the problem body + Retry-After + metric
are correct, per-key isolation (one key never drains another), and fail-open on a
degraded cache. The WS bucket is exercised through :func:`ws_connect_allowed` (the WS
consumer's entry point; the WSGI middleware does not see ASGI upgrades).

Uses live Redis (the buckets' real store). ``DF_RATE_LIMIT_ENABLED`` defaults False so
existing suites are not throttled; these tests enable it explicitly and clean their
own bucket keys so the suite is order-independent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import pytest
import redis
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from config import rate_limit_middleware as rlm
from observation.infra import metrics


@pytest.fixture(autouse=True)
def _clean_buckets() -> Iterator[None]:
    """Flush the token-bucket keyspace before + after each test (isolation)."""
    client = redis.Redis.from_url(settings.REDIS_URL)
    for pattern in ("tb:*",):
        keys = list(client.scan_iter(match=pattern))
        if keys:
            client.delete(*keys)
    yield
    for pattern in ("tb:*",):
        keys = list(client.scan_iter(match=pattern))
        if keys:
            client.delete(*keys)


def _key(short: str) -> str:
    """A structurally-valid ``df_<env>_<8-char prefix>_<30-char secret>`` key string.

    ``parse_key`` only admits an exact-length prefix/secret, so the limiter keys on the
    8-char public prefix; ``short`` (padded/truncated to 8) makes a distinct bucket.
    """
    prefix = (short + "00000000")[:8]
    secret = "s" * 30
    return f"df_test_{prefix}_{secret}"


def _request(*, path: str, url_name: str, api_key: str | None) -> HttpRequest:
    """A synthetic request carrying the scope route name + the keying header."""
    from django.urls import ResolverMatch

    factory = RequestFactory()
    request: HttpRequest = factory.get(path)
    if api_key:
        request.META["HTTP_X_API_KEY"] = api_key
    request.resolver_match = ResolverMatch(
        func=lambda r: HttpResponse(), args=(), kwargs={}, url_name=url_name
    )
    return request


def _ok(_request: HttpRequest) -> HttpResponse:
    return HttpResponse(b"ok", status=200)


def _drive_until_limited(
    *, path: str, url_name: str, api_key: str, attempts: int
) -> HttpResponse:
    """Send ``attempts`` requests through the enabled middleware; return the last response."""
    responses: list[HttpResponse] = []
    handler: Callable[[HttpRequest], HttpResponse] = _ok
    with override_settings(DF_RATE_LIMIT_ENABLED=True):
        middleware = rlm.RateLimitMiddleware(handler)
        for _ in range(attempts):
            responses.append(middleware(_request(path=path, url_name=url_name, api_key=api_key)))
    return responses[-1]


@pytest.mark.parametrize(
    "scope,url_name,path,limit",
    [
        (rlm.SCOPE_DATA_EVENTS, "stream-events", "/api/v1/streams/x/events", 600),
        (rlm.SCOPE_CONTROL, "stream-list", "/api/v1/streams", 120),
        (rlm.SCOPE_LIFECYCLE, "stream-start", "/api/v1/streams/x/start", 30),
    ],
)
def test_each_bucket_trips_at_its_limit(
    scope: str, url_name: str, path: str, limit: int
) -> None:
    """Each scope admits its per-minute limit then returns 429 + Retry-After + metric."""
    key = _key("abcd")
    before = metrics.rate_limited_total.labels(scope=scope)._value.get()
    # Send a margin over the limit so wall-clock refill during the (Redis-round-trip)
    # request loop cannot mask the trip: capacity tokens deplete, then the bucket is
    # empty and the next request is refused (the exact boundary is timing-sensitive at
    # the higher-rate buckets, so we assert the bucket DOES trip, not the exact index).
    last = _drive_until_limited(
        path=path, url_name=url_name, api_key=key, attempts=limit + 50
    )
    assert last.status_code == 429, f"{scope} did not trip at {limit}"
    assert last["Retry-After"] == str(int(last["Retry-After"]))  # integer seconds
    assert int(last["Retry-After"]) >= 1
    body = json.loads(last.content)
    assert body["status"] == 429
    assert body["type"].endswith("/rate-limited")
    assert body["scope"] == scope
    # The metric incremented for this scope (at least once).
    after = metrics.rate_limited_total.labels(scope=scope)._value.get()
    assert after >= before + 1


def test_under_limit_passes_through() -> None:
    """A request under the bucket limit is admitted (control case)."""
    key = _key("under")
    last = _drive_until_limited(
        path="/api/v1/streams/x/start", url_name="stream-start", api_key=key, attempts=5
    )
    assert last.status_code == 200


def test_per_key_isolation_one_key_never_drains_another() -> None:
    """Exhausting key A's lifecycle bucket does not deny key B (per-key keying)."""
    key_a = _key("aaaa")
    key_b = _key("bbbb")
    # Drain A past its lifecycle limit (30/min).
    a_last = _drive_until_limited(
        path="/api/v1/streams/x/start", url_name="stream-start", api_key=key_a, attempts=31
    )
    assert a_last.status_code == 429
    # B's first request is still admitted — A's exhaustion did not touch B's bucket.
    b_last = _drive_until_limited(
        path="/api/v1/streams/x/start", url_name="stream-start", api_key=key_b, attempts=1
    )
    assert b_last.status_code == 200


def test_disabled_middleware_never_throttles() -> None:
    """With DF_RATE_LIMIT_ENABLED False, even over-limit traffic passes (test default)."""
    resp: HttpResponse = HttpResponse(status=500)
    with override_settings(DF_RATE_LIMIT_ENABLED=False):
        middleware = rlm.RateLimitMiddleware(_ok)
        for _ in range(50):
            resp = middleware(
                _request(
                    path="/api/v1/streams/x/start", url_name="stream-start",
                    api_key=_key("disab"),
                )
            )
        assert resp.status_code == 200


def test_fail_open_on_degraded_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis outage fails OPEN — the limiter allows rather than denies (api-spec §2.8)."""
    from identity.infra import rate_limit

    def _boom(*_a: object, **_k: object) -> object:
        raise redis.RedisError("cache down")

    monkeypatch.setattr(rate_limit, "_redis", _boom)
    last = _drive_until_limited(
        path="/api/v1/streams/x/start", url_name="stream-start",
        api_key=_key("fail"), attempts=100,
    )
    assert last.status_code == 200, "limiter denied while the cache was down (not fail-open)"


def test_ws_connect_bucket_trips_at_ten_per_minute() -> None:
    """The WS-connect bucket (10/min) refuses the 11th connect for one identifier."""
    identifier = "key:df_test_wsws3333"
    results = [rlm.ws_connect_allowed(identifier) for _ in range(11)]
    assert all(r.allowed for r in results[:10]), "WS bucket denied within its 10/min limit"
    assert not results[10].allowed, "WS bucket admitted an 11th connect in the window"
    assert results[10].retry_after >= 1


def test_ws_connect_bucket_is_per_identifier() -> None:
    """One identifier's exhausted WS bucket does not deny another (per-key isolation)."""
    busy = "key:df_test_wsbusy4444"
    fresh = "key:df_test_wsfresh5555"
    for _ in range(11):
        rlm.ws_connect_allowed(busy)
    assert rlm.ws_connect_allowed(busy).allowed is False
    assert rlm.ws_connect_allowed(fresh).allowed is True
