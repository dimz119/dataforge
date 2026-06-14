"""PoolStore port adapter — the Tier-2 Redis hot-state write-behind mirror
(database-schema §5.6; behavior-engine §4.1/§4.3; engine port
:class:`dataforge_engine.ports.PoolStore`).

The authoritative working set (Tier 1) is owned by the engine in-process; this
adapter mirrors it into Redis at ``pool:{stream_id}:{shard_id}:{entity_type}``
(a hash, ``entity_key -> json``) so the §5.6 key shapes are populated for
introspection and the Phase-11 cross-shard read path. It is **never** the
interpreter's read path for owned entities, so it is fully write-behind and
fail-soft: a slow or absent Redis costs no correctness (the engine tolerates it).

For batch generation a no-op store is sufficient (the in-process pool is the whole
truth and is snapshotted to Postgres at finalization). :class:`NullPoolStore` is
that no-op; :class:`RedisPoolStore` is the real mirror a streaming host wires.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from dataforge_engine.envelope.types import JSONValue

logger = structlog.get_logger(__name__)

__all__ = ["NullPoolStore", "RedisPoolStore"]

_KEY = "pool:{stream_id}:{shard_id}:{entity_type}"


class NullPoolStore:
    """A no-op :class:`dataforge_engine.ports.PoolStore` (batch / golden / dry-run).

    Tier 1 is authoritative and snapshotted to Postgres; no Redis mirror is needed
    for a single-host bounded batch.
    """

    def flush_records(
        self,
        *,
        entity_type: str,
        upserts: Mapping[str, Mapping[str, JSONValue]],
        deletes: Iterable[str],
    ) -> None:
        return None

    def overwrite(
        self, *, entity_type: str, records: Mapping[str, Mapping[str, JSONValue]]
    ) -> None:
        return None


class RedisPoolStore:
    """The Tier-2 Redis mirror at ``pool:{stream}:{shard}:{type}`` (§5.6).

    Fail-soft: every Redis call is wrapped so a transient error logs and returns
    rather than perturbing generation (the mirror is rebuildable from Tier-1 +
    snapshots; INV — losing Redis loses no durable truth).
    """

    def __init__(self, *, stream_id: str, shard_id: int) -> None:
        self._stream_id = stream_id
        self._shard_id = shard_id

    def _client(self) -> object:
        import redis
        from django.conf import settings

        return redis.Redis.from_url(settings.REDIS_URL)

    def _key(self, entity_type: str) -> str:
        return _KEY.format(
            stream_id=self._stream_id, shard_id=self._shard_id, entity_type=entity_type
        )

    def flush_records(
        self,
        *,
        entity_type: str,
        upserts: Mapping[str, Mapping[str, JSONValue]],
        deletes: Iterable[str],
    ) -> None:
        key = self._key(entity_type)
        try:
            client = self._client()
            pipe = client.pipeline()  # type: ignore[attr-defined]
            if upserts:
                mapping = {
                    k: json.dumps(v, separators=(",", ":")) for k, v in upserts.items()
                }
                pipe.hset(key, mapping=mapping)
            dead = list(deletes)
            if dead:
                pipe.hdel(key, *dead)
            pipe.execute()
        except Exception as exc:  # fail-soft: the mirror is best-effort
            logger.warning("pool_store_flush_failed", key=key, error=str(exc))

    def overwrite(
        self, *, entity_type: str, records: Mapping[str, Mapping[str, JSONValue]]
    ) -> None:
        key = self._key(entity_type)
        try:
            client = self._client()
            pipe = client.pipeline()  # type: ignore[attr-defined]
            pipe.delete(key)
            if records:
                mapping = {
                    k: json.dumps(v, separators=(",", ":")) for k, v in records.items()
                }
                pipe.hset(key, mapping=mapping)
            pipe.execute()
        except Exception as exc:
            logger.warning("pool_store_overwrite_failed", key=key, error=str(exc))
