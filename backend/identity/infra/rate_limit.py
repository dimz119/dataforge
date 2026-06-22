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

import time
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


# --- Per-key token bucket (P11-08; api-spec §2.8) ----------------------------
# A Redis token bucket: tokens refill at ``rate_per_sec`` up to ``capacity``; each
# admitted request consumes one. Smoother than fixed windows (no edge-of-window
# burst doubling) — the api-spec §2.8 per-key data-plane limiter. Evaluated
# atomically in a single Lua script so concurrent requests on one key never
# over-admit (the read-modify-write is server-side).
@dataclass(frozen=True)
class TokenBucket:
    """A token bucket: ``capacity`` tokens, refilling at ``rate_per_sec`` tokens/s."""

    capacity: int
    rate_per_sec: float


# KEYS[1] = bucket key. ARGV: capacity, rate_per_sec, now_ms, ttl_seconds.
# Returns {allowed (1/0), retry_after_ms}. Stores tokens + last-refill ms in a hash.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now_ms
end
local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
local retry_after_ms = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
else
  retry_after_ms = math.ceil((1.0 - tokens) / rate * 1000.0)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl)
return {allowed, retry_after_ms}
"""


def check_token_bucket(scope: str, identifier: str, bucket: TokenBucket) -> RateLimitResult:
    """Consume one token from ``scope``:``identifier``'s bucket (P11-08).

    Atomic (one Lua eval) so concurrent requests on one key never over-admit.
    ``retry_after`` is whole seconds until one token refills (≥ 1 when denied).
    Fails **open** on a degraded cache (logs + allows) — a Redis outage must not
    deny legitimate traffic, the same stance as the fixed-window limiter above.
    """
    if bucket.capacity <= 0 or bucket.rate_per_sec <= 0:
        return RateLimitResult(allowed=True)
    key = f"tb:{scope}:{identifier}"
    # The bucket key lives long enough to refill from empty to full, plus a margin,
    # so an idle key self-prunes without losing in-flight refill state.
    ttl_ms = int((bucket.capacity / bucket.rate_per_sec + 60) * 1000)
    now_ms = int(time.time() * 1000)
    try:
        client = _redis()
        # ARGV are passed as strings (the Lua ``tonumber`` parses them); redis-py's
        # stubs type eval's argv as str even though it accepts numbers at runtime.
        result = cast(
            "list[int]",
            client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                str(bucket.capacity),
                str(bucket.rate_per_sec),
                str(now_ms),
                str(ttl_ms),
            ),
        )
        allowed = bool(result[0])
        if allowed:
            return RateLimitResult(allowed=True)
        retry_after = max(1, (int(result[1]) + 999) // 1000)
        return RateLimitResult(allowed=False, retry_after=retry_after)
    except redis.RedisError as exc:
        logger.warning("rate_limit_degraded", scope=scope, error=str(exc))
        return RateLimitResult(allowed=True)
