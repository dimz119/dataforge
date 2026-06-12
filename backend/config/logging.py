"""Structured JSON logging — the shared structlog processor chain (observability §2).

Every process (`web`, `ws`, `worker`, `beat`, `runner`, `buffer-writer`,
`ws-pusher`) emits one JSON object per line to stdout. The chain is defined
once here; no process configures logging independently (observability §2.1).
"""

from __future__ import annotations

import datetime
import logging
import sys

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

_configured = False


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask secret-bearing keys at the top level and inside `ctx` (observability §2.2)."""
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = _REDACTED
    ctx = event_dict.get("ctx")
    if isinstance(ctx, dict):
        for key in list(ctx):
            if key.lower() in _REDACTED_KEYS:
                ctx[key] = _REDACTED
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
