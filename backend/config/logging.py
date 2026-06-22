"""Structured JSON logging — the shared structlog processor chain (observability §2).

Every process (`web`, `ws`, `worker`, `beat`, `runner`, `buffer-writer`,
`ws-pusher`) emits one JSON object per line to stdout. The chain is defined
once here; no process configures logging independently (observability §2.1).
"""

from __future__ import annotations

import datetime
import logging
import sys
import threading
import time

import structlog
from structlog.types import EventDict, WrappedLogger

# Keys whose values must never reach a log line (observability §2.2 redaction
# rules): API-key secrets/hashes, JWTs, passwords and hashes, verification or
# reset tokens, Authorization headers. `api_key_id` (id + prefix only) is fine.
_REDACTED_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "jwt",
        "key_hash",
        "password",
        "password_hash",
        "refresh_token",
        "reset_token",
        "secret",
        "token",
        "token_hash",
        "verification_token",
    }
)

_REDACTED = "[redacted]"

# Prefix that marks a full DataForge API-key secret string (``df_<env>_<prefix>_<secret>``).
# If a raw key string ever reaches a log call (a bug we still defend against), the
# redaction processor collapses it to its public ``prefix…last4`` handle rather than
# emitting the secret (observability §2.2: keys appear as prefix…last4 only).
_API_KEY_LITERAL_PREFIX = "df_"


def public_api_key_handle(raw_or_prefix: str, last4: str | None = None) -> str:
    """Render an API key as its public ``prefix…last4`` handle (observability §2.2).

    ``raw_or_prefix`` may be either the durable ``key_prefix`` (``df_<env>_<short>``)
    or a full secret string (``df_<env>_<short>_<secret>``). In both cases only the
    public ``<short>`` segment (index 2) and the trailing four characters survive;
    the secret body (index 3+) is never emitted. ``last4`` overrides the trailing
    four when the caller already has the stored value rather than the raw string.
    """
    parts = raw_or_prefix.split("_")
    # The public short id is the third segment of ``df_<env>_<short>[_<secret>]``;
    # everything from index 3 on is the secret body and must be dropped.
    short = parts[2] if len(parts) >= 3 else parts[-1]
    if last4 is not None:
        tail = last4
    elif len(parts) >= 4:
        # Full secret string: the last 4 of the secret body identify the key.
        tail = parts[-1][-4:]
    else:
        tail = raw_or_prefix[-4:]
    return f"{short}…{tail}"


def _mask_api_key_literal(value: object) -> object:
    """Collapse any leaked full-key string into its public handle; else passthrough."""
    if (
        isinstance(value, str)
        and value.startswith(_API_KEY_LITERAL_PREFIX)
        and value.count("_") >= 3
    ):
        return public_api_key_handle(value)
    return value


_configured = False


def _redact_mapping(mapping: dict[str, object]) -> None:
    """Mask secret-bearing keys in-place; recurse into nested dicts (observability §2.2)."""
    for key in list(mapping):
        if key.lower() in _REDACTED_KEYS:
            mapping[key] = _REDACTED
            continue
        value = mapping[key]
        if isinstance(value, dict):
            _redact_mapping(value)
        else:
            mapping[key] = _mask_api_key_literal(value)


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask secret-bearing keys at the top level and inside `ctx` (observability §2.2).

    Redacted: API-key secrets/hashes, JWTs, passwords/hashes, verification/reset
    tokens, Authorization headers. ``api_key_id``/``api_key_prefix``/``api_key_last4``
    survive (id + prefix…last4 only). Any full-key string that leaks into a value is
    collapsed to its public handle as a last line of defence.
    """
    _redact_mapping(event_dict)  # type: ignore[arg-type]
    return event_dict


def _timestamper(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """RFC 3339 UTC timestamp with millisecond precision in `ts` (observability §2.2)."""
    now = datetime.datetime.now(datetime.UTC)
    event_dict["ts"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return event_dict


def _normalise_event(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Frozen field schema defaults: `event`, `message`, nullable tenant fields (§2.2)."""
    event_dict.setdefault("message", event_dict.get("event", ""))
    event_dict.setdefault("workspace_id", None)
    event_dict.setdefault("stream_id", None)
    return event_dict


def _format_error(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Escape exceptions into `error.kind` / `error.message` / `error.stack` (§2.2)."""
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc: BaseException | None = exc_info
    else:
        exc = exc_info[1] if isinstance(exc_info, tuple) else None
    if exc is not None:
        import traceback

        event_dict["error.kind"] = type(exc).__name__
        event_dict["error.message"] = str(exc)
        event_dict["error.stack"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    return event_dict


def _service_context(
    service: str, env_name: str, release: str
) -> structlog.types.Processor:
    def add_context(
        _logger: WrappedLogger, _method: str, event_dict: EventDict
    ) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict["env"] = env_name
        event_dict["release"] = release
        return event_dict

    return add_context


def configure_logging(
    *,
    service: str,
    env_name: str,
    release: str,
    level: str = "INFO",
    per_logger_levels: str = "",
) -> None:
    """Configure structlog + stdlib logging for one process.

    `per_logger_levels` is the `DF_LOG_LEVELS` syntax of observability §2.1,
    e.g. ``dataforge.chaos=DEBUG,delivery.api=INFO``.
    """
    global _configured

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _service_context(service, env_name, release),
        _timestamper,
        _format_error,
        _normalise_event,
        _redact,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=not _configured,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    for entry in per_logger_levels.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, logger_level = entry.partition("=")
        logging.getLogger(name.strip()).setLevel(logger_level.strip().upper())

    _configured = True


# ---------------------------------------------------------------------------
# Context binding (observability §3.1)
# ---------------------------------------------------------------------------
# The web middleware binds request_id/trace_id/span_id directly; data-plane and
# worker code uses these helpers so every line below the bind carries the tenant
# correlation fields of the frozen schema. ``None`` values are dropped so the
# frozen-schema defaults (nullable) apply instead of overriding with null.
_BINDABLE = ("request_id", "workspace_id", "stream_id", "shard_id", "user_id", "api_key_id")


def bind_log_context(
    *,
    request_id: str | None = None,
    workspace_id: str | None = None,
    stream_id: str | None = None,
    shard_id: int | None = None,
    user_id: str | None = None,
    api_key_id: str | None = None,
) -> None:
    """Bind correlation fields into structlog contextvars (observability §3.1).

    Only non-``None`` fields are bound. ``api_key_id`` is the key *id* (a UUID),
    never a secret/hash — the redaction processor still defends the value.
    """
    bound = {
        key: value
        for key, value in {
            "request_id": request_id,
            "workspace_id": workspace_id,
            "stream_id": stream_id,
            "shard_id": shard_id,
            "user_id": user_id,
            "api_key_id": api_key_id,
        }.items()
        if value is not None
    }
    if bound:
        structlog.contextvars.bind_contextvars(**bound)


def unbind_log_context(*keys: str) -> None:
    """Unbind correlation fields (defaults to every bindable field)."""
    structlog.contextvars.unbind_contextvars(*(keys or _BINDABLE))


# ---------------------------------------------------------------------------
# Volume rules LV-1..4 (observability §2.2)
# ---------------------------------------------------------------------------
# The data plane is high-frequency; raw per-tick INFO lines would drown the log.
# These helpers enforce the frozen volume budget without callers tracking state.


class _RateLimiter:
    """Per-key wall-clock throttle (LV-1 / LV-4 windows). Process-local, thread-safe."""

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._last: dict[object, float] = {}
        self._suppressed: dict[object, int] = {}
        self._lock = threading.Lock()

    def admit(self, key: object, *, now: float | None = None) -> tuple[bool, int]:
        """Return ``(allowed, suppressed_since_last_emit)`` for ``key``.

        When allowed, the suppressed counter resets to 0; when suppressed it is
        incremented and the call returns ``(False, _)``.
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is None or (moment - last) >= self._window:
                suppressed = self._suppressed.pop(key, 0)
                self._last[key] = moment
                return True, suppressed
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, self._suppressed[key]


# LV-1: data-plane tick summary — ≤ 1 INFO per stream per 60 s.
_TICK_LIMITER = _RateLimiter(60.0)
# LV-4: WARNING+ dedup — one line per (event, workspace_id, stream_id) per 60 s.
_WARN_DEDUP_LIMITER = _RateLimiter(60.0)


def emit_tick_summary(
    logger: structlog.stdlib.BoundLogger,
    *,
    stream_id: str,
    shard_id: int,
    **fields: object,
) -> None:
    """LV-1: emit at most one ``runner.tick.summary`` INFO per stream per 60 s.

    Callers pass the rolled-up counters for the window (events emitted, ticks,
    overruns, lag) in ``fields``; intermediate ticks are silently dropped. This is
    the *only* sanctioned data-plane INFO line.
    """
    allowed, _ = _TICK_LIMITER.admit(stream_id)
    if allowed:
        logger.info(
            "runner.tick.summary",
            stream_id=stream_id,
            shard_id=shard_id,
            **fields,
        )


def emit_stream_state_changed(
    logger: structlog.stdlib.BoundLogger,
    *,
    stream_id: str,
    from_state: str,
    to_state: str,
    reason: str,
    **fields: object,
) -> None:
    """LV-3: one INFO ``stream.state.changed`` per lifecycle transition (always emitted)."""
    logger.info(
        "stream.state.changed",
        stream_id=stream_id,
        **{"from": from_state, "to": to_state, "reason": reason},
        **fields,
    )


def emit_deduped_warning(
    logger: structlog.stdlib.BoundLogger,
    event: str,
    *,
    level: str = "warning",
    workspace_id: str | None = None,
    stream_id: str | None = None,
    **fields: object,
) -> None:
    """LV-4: emit a WARNING+ line at most once per (event, workspace_id, stream_id)/60 s.

    Suppressed repeats are counted; the next emitted line in the window carries
    ``suppressed_count`` so the cardinality of an alert storm is preserved without
    one-line-per-occurrence noise. ``level`` is ``warning``/``error``/``critical``.
    """
    key = (event, workspace_id, stream_id)
    allowed, suppressed = _WARN_DEDUP_LIMITER.admit(key)
    if not allowed:
        return
    log = getattr(logger, level.lower())
    payload: dict[str, object] = {"workspace_id": workspace_id, "stream_id": stream_id, **fields}
    if suppressed:
        payload["suppressed_count"] = suppressed
    log(event, **payload)


def reset_volume_limiters() -> None:
    """Reset LV-1/LV-4 throttle state (test hook; not used in production)."""
    _TICK_LIMITER._last.clear()
    _TICK_LIMITER._suppressed.clear()
    _WARN_DEDUP_LIMITER._last.clear()
    _WARN_DEDUP_LIMITER._suppressed.clear()
