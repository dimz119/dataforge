"""Lease-presence reads for the control-plane watchdog (backend-architecture §8.2).

The control plane is NOT the lease authority — the runner data plane acquires,
heartbeats, and releases leases in Redis (``runner.leases``). The watchdog only
*reads* whether a live lease exists for a shard, to decide the T4/T11 failed
transition (no live lease past the failover window). The lease key contract is
fixed by §8.2:

* ``df:lease:{stream_id}:{shard_id}`` → JSON ``{runner_id, fencing_token}``; set
  ``SET NX PX 15000`` by the runner, refreshed every 5 s. Its mere existence (a
  non-expired key) means a runner holds the shard.

On any Redis error the watchdog treats the lease as **present** (conservative:
never declare a stream failed because Redis is degraded — fail safe toward "still
running", SEC-style fail-closed against false-positive failure). Postgres remains
the fencing-token authority (``stream_shards``), so this read is advisory.
"""

from __future__ import annotations

from uuid import UUID

import redis
import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

# §8.2 lease key template (Redis-resident; the runner is the authority).
_LEASE_KEY = "df:lease:{stream_id}:{shard_id}"

__all__ = ["has_live_lease", "lease_key"]


def lease_key(stream_id: UUID | str, shard_id: int) -> str:
    """The §8.2 Redis lease key for a (stream, shard)."""
    return _LEASE_KEY.format(stream_id=stream_id, shard_id=shard_id)


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def has_live_lease(stream_id: UUID | str, shard_id: int) -> bool:
    """True iff a non-expired Redis lease exists for the shard (§8.2).

    On a Redis error returns ``True`` (conservative — the watchdog must not declare
    a stream failed on a transient cache outage). A missing key (TTL expired, the
    runner crashed) returns ``False`` — the failover signal.
    """
    try:
        return bool(_client().exists(lease_key(stream_id, shard_id)))
    except redis.RedisError as exc:
        logger.warning("lease_presence_read_degraded", stream_id=str(stream_id), error=str(exc))
        return True  # fail safe: never fail a stream on a degraded cache
