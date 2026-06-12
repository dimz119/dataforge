"""Readiness evaluation (observability §6).

Dependency probes run with a 2 s timeout each and results are cached for 5 s.
HTTP 200 iff every *gating* component passes; non-gating components are
reported in the map but never flip readiness — e.g. a Kafka outage must not
take the control-plane API out of rotation (§6.1).
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from observation.infra import probes

PROBE_TIMEOUT_S = 2.0
CACHE_TTL_S = 5.0

_PROBES: dict[str, Callable[[], None]] = {
    "postgres": probes.probe_postgres,
    "redis": probes.probe_redis,
    "kafka": probes.probe_kafka,
    "migrations": probes.probe_migrations,
}

# Per-process gating sets (observability §6.2): {service: (gating, reported)}.
GATING_SETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "web": (("postgres", "redis", "migrations"), ("kafka",)),
    "ws": (("redis", "postgres"), ("kafka",)),
    "worker": (("redis", "postgres"), ("kafka",)),
    "beat": (("redis",), ()),
    "runner": (("postgres", "redis", "kafka"), ()),
    "buffer-writer": (("kafka", "postgres"), ("redis",)),
    "ws-pusher": (("kafka", "redis"), ("postgres",)),
}

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="readyz-probe")
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    components: dict[str, str]
    gating: list[str]


def reset_cache() -> None:
    """Drop cached probe results (test hook)."""
    with _lock:
        _cache.clear()


def _probe(component: str) -> str:
    """Run one probe with the 2 s timeout; cache the result for 5 s (§6.1)."""
    now = time.monotonic()
    with _lock:
        cached = _cache.get(component)
        if cached is not None and now - cached[0] < CACHE_TTL_S:
            return cached[1]

    future = _executor.submit(_PROBES[component])
    try:
        future.result(timeout=PROBE_TIMEOUT_S)
        result = "ok"
    except FutureTimeoutError:
        future.cancel()
        result = "timeout"
    except Exception as exc:
        result = f"error: {type(exc).__name__}"

    with _lock:
        _cache[component] = (time.monotonic(), result)
    return result


def evaluate(service: str) -> ReadinessReport:
    """Probe the components for `service`'s gating set and report per §6.1."""
    gating, reported = GATING_SETS.get(service, GATING_SETS["web"])
    components = {component: _probe(component) for component in (*gating, *reported)}
    ready = all(components[component] == "ok" for component in gating)
    return ReadinessReport(ready=ready, components=components, gating=list(gating))
