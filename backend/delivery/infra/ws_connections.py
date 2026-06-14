"""WebSocket connection-quota registry (delivery-channels §6.2 WS-4; api-spec §5.1).

Redis-backed concurrency counters that enforce the per-key (5) and per-workspace
(250) live-connection limits (WS-4). Each open connection registers a unique member
in two Redis sets — ``ws:conns:key:{api_key_id}`` and ``ws:conns:ws:{workspace_id}``
— with a refresh TTL so a crashed connection's slot self-heals (the heartbeat tick
re-registers; a dead socket's members expire). Admission is atomic-ish: register
then check the cardinality and roll back if over the cap (the small race admits at
most one extra under a thundering reconnect, well within an abuse-control tolerance).

The quota is a connection-admission control, not an auth primitive, so it **fails
open** on Redis errors (a Redis outage must not deny legitimate tails) — the security
fail-closed stance applies to credential checks (``ws_auth``), not this counter,
mirroring the REST rate-limiter (identity §5.4).

JWT callers have no API-key dimension, so only the per-workspace cap applies to them
(``api_key_id=None`` skips the per-key set).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import redis
import structlog
from django.conf import settings

from delivery.domain.ws_protocol import (
    MAX_CONNECTIONS_PER_KEY,
    MAX_CONNECTIONS_PER_WORKSPACE,
)

logger = structlog.get_logger("dataforge.delivery.ws_connections")

__all__ = ["ConnectionSlot", "admit_connection", "refresh_connection", "release_connection"]

# A connection's slot lives this long without a refresh; the heartbeat tick (15 s,
# WS-12) re-registers well inside it, so a live socket never expires and a dead one
# self-heals within the TTL.
_SLOT_TTL_S = 45

_KEY_SET = "ws:conns:key:{api_key_id}"
_WS_SET = "ws:conns:ws:{workspace_id}"


@dataclass(frozen=True)
class ConnectionSlot:
    """A held connection slot (the members registered for one live connection)."""

    connection_id: str
    workspace_id: str
    api_key_id: str | None


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def admit_connection(
    *, connection_id: str, workspace_id: str, api_key_id: str | None
) -> bool:
    """Register the connection and admit it iff under both caps (WS-4).

    Returns ``True`` if admitted (slot held — the caller MUST
    :func:`release_connection` on close), ``False`` if a cap is exceeded (the caller
    closes ``4429``; any partial registration is rolled back). Fails **open** (admits)
    on a Redis error.
    """
    try:
        client = _redis()
        ws_set = _WS_SET.format(workspace_id=workspace_id)
        client.sadd(ws_set, connection_id)
        client.expire(ws_set, _SLOT_TTL_S)
        ws_count = cast("int", client.scard(ws_set))  # sync client → int (not awaitable)
        key_count = 0
        if api_key_id is not None:
            key_set = _KEY_SET.format(api_key_id=api_key_id)
            client.sadd(key_set, connection_id)
            client.expire(key_set, _SLOT_TTL_S)
            key_count = cast("int", client.scard(key_set))

        over = ws_count > MAX_CONNECTIONS_PER_WORKSPACE or (
            api_key_id is not None and key_count > MAX_CONNECTIONS_PER_KEY
        )
        if over:
            _release(client, connection_id, workspace_id, api_key_id)
            return False
    except redis.RedisError as exc:
        logger.warning("ws_connections.admit_degraded", error=str(exc))
        return True  # fail open — admission control, not auth
    return True


def refresh_connection(slot: ConnectionSlot) -> None:
    """Re-register + re-TTL the slot on each heartbeat so a live socket never expires."""
    try:
        client = _redis()
        ws_set = _WS_SET.format(workspace_id=slot.workspace_id)
        client.sadd(ws_set, slot.connection_id)
        client.expire(ws_set, _SLOT_TTL_S)
        if slot.api_key_id is not None:
            key_set = _KEY_SET.format(api_key_id=slot.api_key_id)
            client.sadd(key_set, slot.connection_id)
            client.expire(key_set, _SLOT_TTL_S)
    except redis.RedisError as exc:
        logger.warning("ws_connections.refresh_degraded", error=str(exc))


def release_connection(slot: ConnectionSlot) -> None:
    """Drop the connection's members from both sets on close (best-effort)."""
    try:
        _release(_redis(), slot.connection_id, slot.workspace_id, slot.api_key_id)
    except redis.RedisError as exc:
        logger.warning("ws_connections.release_degraded", error=str(exc))


def _release(
    client: redis.Redis, connection_id: str, workspace_id: str, api_key_id: str | None
) -> None:
    client.srem(_WS_SET.format(workspace_id=workspace_id), connection_id)
    if api_key_id is not None:
        client.srem(_KEY_SET.format(api_key_id=api_key_id), connection_id)
