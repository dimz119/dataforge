"""Redis-backed rate limiter for the auth abuse controls (security §5.4).

Signup is the front door (RL-1): 5/h + 20/day per IP. Implemented as fixed
windows in Redis (`INCR` + `EXPIRE`) keyed per IP. The limiter is an
abuse-control supplement, not an authentication primitive: if Redis is
unavailable it fails **open** (logs and allows) so a Redis outage never locks
legitimate users out of signup — the security stance for auth correctness
(fail-closed) applies to credential checks, not to this counter.

IP keys honor `Fly-Client-IP` from the trusted edge only (security §5.4);
client-controlled `X-Forwarded-For` is never trusted for keying.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import redis
import structlog
from django.conf import settings
from django.http import HttpRequest

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Window:
    """A fixed-window limit: `limit` events per `seconds`."""

    limit: int
    seconds: int


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a check; `retry_after` is seconds until the tripped window resets."""

    allowed: bool
    retry_after: int = 0


def client_ip(request: HttpRequest) -> str:
    """Resolve the caller IP from the trusted edge header only (security §5.4).

    Trusts `Fly-Client-IP` (set by the Fly edge); falls back to `REMOTE_ADDR`.
    Never trusts client-supplied `X-Forwarded-For` for abuse keying.
    """
    fly = request.headers.get("Fly-Client-IP")
    if fly:
        return fly.strip()
    return str(request.META.get("REMOTE_ADDR", "0.0.0.0"))


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def check(scope: str, identifier: str, windows: tuple[Window, ...]) -> RateLimitResult:
    """Atomically count this event against every window; deny if any is exhausted.

    Counts only when all windows are still under limit (so a single request never
    consumes budget from a window that another window already tripped).
    """
    if not windows:
        return RateLimitResult(allowed=True)
    try:
        client = _redis()
        keys = [f"ratelimit:{scope}:{w.seconds}:{identifier}" for w in windows]
        raw = cast("list[bytes | None]", client.mget(keys))
        counts = [int(v or 0) for v in raw]
        for window, count in zip(windows, counts, strict=True):
            if count >= window.limit:
                ttl_raw = cast(int, client.ttl(f"ratelimit:{scope}:{window.seconds}:{identifier}"))
                return RateLimitResult(allowed=False, retry_after=max(1, ttl_raw))
        # Under all limits → consume one token from each window.
        pipe = client.pipeline()
        for window, key in zip(windows, keys, strict=True):
            pipe.incr(key)
            pipe.expire(key, window.seconds, nx=True)
        pipe.execute()
        return RateLimitResult(allowed=True)
    except redis.RedisError as exc:
        # Degraded limiter must not deny legitimate auth flows: fail open.
        logger.warning("rate_limit_degraded", scope=scope, error=str(exc))
        return RateLimitResult(allowed=True)


def signup_windows() -> tuple[Window, ...]:
    """RL-1 signup windows from settings (security §5.4)."""
    return (
        Window(limit=settings.SIGNUP_RATE_LIMIT_PER_HOUR, seconds=3600),
        Window(limit=settings.SIGNUP_RATE_LIMIT_PER_DAY, seconds=86400),
    )
