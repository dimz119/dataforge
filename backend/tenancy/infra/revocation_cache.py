"""Redis API-key revocation cache + last-used write-behind (security §3.2.3/4).

The cache that makes revocation effective < 1 s platform-wide (SEC-KEY-5): every
verification consults ``apikey:state:{prefix}`` first.

* **active** entries carry TTL **60 s** (SEC-KEY-6) — worst-case staleness if a
  synchronous revoke write fails is 60 s, after which the DB truth wins.
* **revoked** entries are written **synchronously before the 204** on revoke,
  TTL **48 h** (SEC-KEY-5), so the revoked key is rejected immediately.
* **fail closed to slower, never to allow** (SEC-KEY-7): on any Redis error the
  caller falls back to the database; a cache miss is *not* "allow".

``last_used_at`` is write-behind (SEC-KEY-9): verification touches
``apikey:last_used:{api_key_id}`` in Redis; a Celery beat task (Phase 11) flushes
to Postgres at minute precision — losing a window loses telemetry, never auth
correctness.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import cast

import redis
import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

_STATE_KEY = "apikey:state:{prefix}"
_LAST_USED_KEY = "apikey:last_used:{api_key_id}"
_ACTIVE_TTL = 60  # SEC-KEY-6
_REVOKED_TTL = 48 * 3600  # SEC-KEY-5

STATE_ACTIVE = "active"
STATE_REVOKED = "revoked"


@dataclass(frozen=True)
class CachedKeyState:
    """A cached active-key record (security §3.2.3): the verify inputs."""

    api_key_id: str
    workspace_id: str
    key_hash: str
    scopes: list[str]


class RevocationCacheError(RuntimeError):
    """A required synchronous cache write failed (revoke path; SEC-KEY-6)."""


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def get_state(prefix: str) -> str | CachedKeyState | None:
    """Return the cached state for ``prefix``.

    ``STATE_REVOKED`` (string) ⇒ reject; a ``CachedKeyState`` ⇒ active inputs;
    ``None`` ⇒ cache miss (caller must consult the DB — never treat as allow).
    On Redis error returns ``None`` (fail to slower DB path, SEC-KEY-7).
    """
    try:
        raw = cast("bytes | None", _client().get(_STATE_KEY.format(prefix=prefix)))
    except redis.RedisError as exc:
        logger.warning("revocation_cache_read_degraded", error=str(exc))
        return None
    if raw is None:
        return None
    payload = json.loads(raw)
    if payload.get("state") == STATE_REVOKED:
        return STATE_REVOKED
    return CachedKeyState(
        api_key_id=payload["api_key_id"],
        workspace_id=payload["workspace_id"],
        key_hash=payload["key_hash"],
        scopes=list(payload.get("scopes", [])),
    )


def put_active(prefix: str, state: CachedKeyState) -> None:
    """Cache an active key for 60 s after a DB miss (security §3.2.3)."""
    payload = json.dumps(
        {
            "state": STATE_ACTIVE,
            "api_key_id": state.api_key_id,
            "workspace_id": state.workspace_id,
            "key_hash": state.key_hash,
            "scopes": state.scopes,
        }
    )
    try:
        _client().set(_STATE_KEY.format(prefix=prefix), payload, ex=_ACTIVE_TTL)
    except redis.RedisError as exc:
        # Active-cache write failure is non-fatal: the next verify re-reads the DB.
        logger.warning("revocation_cache_write_degraded", error=str(exc))


def put_revoked(prefix: str) -> None:
    """Synchronously mark ``prefix`` revoked before the 204 (SEC-KEY-5).

    Raises ``RevocationCacheError`` on failure so the revoke flow can enqueue the
    Celery retry + audit ``tenancy.api_key.revocation_cache_degraded`` (SEC-KEY-6)
    while still committing the DB truth.
    """
    payload = json.dumps({"state": STATE_REVOKED})
    try:
        _client().set(_STATE_KEY.format(prefix=prefix), payload, ex=_REVOKED_TTL)
    except redis.RedisError as exc:
        logger.warning("revocation_cache_revoke_write_failed", error=str(exc))
        raise RevocationCacheError(str(exc)) from exc


def touch_last_used(api_key_id: uuid.UUID | str) -> None:
    """Write-behind last-used timestamp marker (SEC-KEY-9); best-effort."""
    try:
        client = _client()
        key = _LAST_USED_KEY.format(api_key_id=str(api_key_id))
        # Store the marker; the Phase 11 flush task reads + clears it at minute
        # precision. TTL bounds orphaned markers if the flush task is down.
        client.set(key, "1", ex=2 * 3600)
    except redis.RedisError as exc:
        logger.warning("last_used_write_behind_degraded", error=str(exc))
