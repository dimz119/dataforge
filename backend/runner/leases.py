"""Redis lease acquisition, heartbeat, and fencing tokens (backend-architecture §8.2).

This is the **data-plane** lease authority (ADR-0006). The runner supervisor owns
Redis leases; the control-plane watchdog (``streams.infra.leases.has_live_lease``)
only *reads* their presence. The §8.2 contract, implemented exactly here:

==================  ===================================================================
Element             Value
==================  ===================================================================
Lease key           ``df:lease:{stream_id}:{shard_id}`` → JSON ``{runner_id, fencing_token}``
Acquire             ``SET key value NX PX 15000``; on success the token was first
                    obtained via ``INCR df:fence:{stream_id}:{shard_id}`` (strictly
                    monotonic per shard across all time)
Heartbeat           one process task renews **all** held leases every 5 s via a Lua
                    script (compare ``runner_id``, then ``PEXPIRE 15000``); a failed
                    renewal cancels that shard's worker before its next pipeline step
Claimable scan      every 2 s: candidate shards with no live lease
Release             explicit compare-owner Lua ``DEL`` at ``stopped`` finalize;
                    otherwise TTL expiry is the failover signal
==================  ===================================================================

Async by design: the supervisor is an asyncio program (§8.1), so this uses
``redis.asyncio``. The fencing-token kernel and :class:`FencingError` live in
:mod:`runner.fencing`; this module re-exports them so the lease API is one import.

The module imports neither Django models nor ORM seams — it is handed candidate
``(stream_id, shard_id)`` pairs (the runner derives them from
``streams.application.desired_state.claimable_desired_states()`` and each
``DesiredState.shard_count``) and speaks only to Redis. That keeps the lease unit
self-contained and testable against fakeredis.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from runner.fencing import (
    FencingError,
    enforce_conditional_write,
    fence_key,
    is_fresh_token,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redis.asyncio import Redis

__all__ = [
    "DEFAULT_LEASE_KEY",
    "LEASE_TTL_MS",
    "FencingError",
    "Lease",
    "LeaseManager",
    "ShardKey",
    "enforce_conditional_write",
    "fence_key",
    "is_fresh_token",
    "lease_key",
]

# §8.2: SET … PX 15000. The TTL is the failover budget — a holder must renew within
# it (5 s heartbeat ⇒ two missed beats before expiry). Kept as a module constant so
# the heartbeat PEXPIRE and the acquire SET agree on one source of truth.
LEASE_TTL_MS = 15_000

# §8.2 lease key template (matches streams.infra.leases for the watchdog read).
DEFAULT_LEASE_KEY = "df:lease:{stream_id}:{shard_id}"


def lease_key(stream_id: UUID | str, shard_id: int) -> str:
    """The §8.2 Redis lease key for a (stream, shard)."""
    return DEFAULT_LEASE_KEY.format(stream_id=stream_id, shard_id=shard_id)


@dataclass(frozen=True, slots=True)
class ShardKey:
    """A (stream, shard) identity — the unit a lease is held over."""

    stream_id: str
    shard_id: int

    @classmethod
    def of(cls, stream_id: UUID | str, shard_id: int) -> ShardKey:
        return cls(stream_id=str(stream_id), shard_id=shard_id)

    @property
    def lease_key(self) -> str:
        return lease_key(self.stream_id, self.shard_id)

    @property
    def fence_key(self) -> str:
        return fence_key(self.stream_id, self.shard_id)


@dataclass(frozen=True, slots=True)
class Lease:
    """A held lease: the shard, the issuing runner, and its fencing token.

    ``fencing_token`` is the value the shard worker carries into every durable write
    (checkpoint conditional write, ledger/injection idempotent insert) so a zombie's
    stale token is rejected (§8.2 enforcement table).
    """

    shard: ShardKey
    runner_id: str
    fencing_token: int

    @property
    def value(self) -> str:
        """The JSON payload stored at the lease key (``{runner_id, fencing_token}``)."""
        return json.dumps(
            {"runner_id": self.runner_id, "fencing_token": self.fencing_token},
            separators=(",", ":"),
            sort_keys=True,
        )


# -- Lua scripts (atomic compare-owner operations) --------------------------------
#
# Both renew (heartbeat) and release must be *compare-then-act* atomically so a
# zombie cannot renew/delete a lease a new holder now owns. A Redis Lua script runs
# atomically on the server, which is exactly the §8.2 "compare runner_id, then …"
# requirement. We compare the stored JSON's runner_id rather than the whole value so
# a fencing-token bump (which only happens on a fresh acquire under NX) never matters
# to the owner check.

# KEYS[1] = lease key ; ARGV[1] = runner_id ; ARGV[2] = ttl_ms
# Returns 1 if renewed (we own it), 0 if not (missing or owned by another runner).
_RENEW_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, decoded = pcall(cjson.decode, v)
if not ok or decoded['runner_id'] ~= ARGV[1] then return 0 end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""

# KEYS[1] = lease key ; ARGV[1] = runner_id
# Returns 1 if deleted (we owned it), 0 otherwise. Compare-owner DEL (§8.2 release).
_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, decoded = pcall(cjson.decode, v)
if not ok or decoded['runner_id'] ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class LeaseManager:
    """Owns this runner's leases against Redis (§8.2). One per runner process.

    Tracks the set of currently-held leases so :meth:`heartbeat` can renew **all**
    of them in one round-trip-batched operation and report which were lost, and so
    :meth:`shutdown` can release them. The supervisor wires the lost-lease set to
    worker cancellation (a lost lease ⇒ cancel that shard's worker before its next
    pipeline step — INV-STR-2's "stop emitting before the new holder's first tick").
    """

    def __init__(self, redis: Redis, runner_id: str, *, ttl_ms: int = LEASE_TTL_MS) -> None:
        self._redis = redis
        self.runner_id = runner_id
        self._ttl_ms = ttl_ms
        # shard -> held Lease. The authoritative local view of what we own.
        self._held: dict[ShardKey, Lease] = {}
        self._renew = redis.register_script(_RENEW_LUA)
        self._release = redis.register_script(_RELEASE_LUA)

    @property
    def held(self) -> Mapping[ShardKey, Lease]:
        """Read-only view of the leases this runner currently believes it holds."""
        return dict(self._held)

    # -- acquire ------------------------------------------------------------------

    async def acquire(self, stream_id: UUID | str, shard_id: int) -> Lease | None:
        """Try to claim (stream, shard): ``INCR`` the fence, then ``SET NX PX`` (§8.2).

        Returns the :class:`Lease` (with its fresh fencing token) on success, or
        ``None`` if another runner already holds a live lease (the ``NX`` blocks).

        Ordering is fence-first by design: the token is *strictly monotonic per shard
        across all time* (a never-reset ``INCR``), so even a failed ``SET NX`` still
        burns a token — which is harmless (tokens need only be monotonic, not gap-free)
        and guarantees a later acquirer always carries a strictly-greater token than
        any prior holder, which is what fences the zombie. The token is the lease
        *value*, so a winning ``SET NX`` atomically binds the token to the holder.
        """
        shard = ShardKey.of(stream_id, shard_id)
        token = int(await self._redis.incr(shard.fence_key))
        lease = Lease(shard=shard, runner_id=self.runner_id, fencing_token=token)
        won = await self._redis.set(shard.lease_key, lease.value, nx=True, px=self._ttl_ms)
        if not won:
            return None
        self._held[shard] = lease
        return lease

    # -- heartbeat ----------------------------------------------------------------

    async def heartbeat(self) -> list[ShardKey]:
        """Renew **all** held leases (§8.2, 5 s cadence). Return the ones we lost.

        Each renewal is an atomic compare-owner ``PEXPIRE`` (the ``_RENEW_LUA``
        script). A renewal returning ``0`` means the lease is gone or now owned by
        another runner — this runner is a zombie for that shard. Lost shards are
        dropped from the held set and returned so the supervisor cancels their
        workers before the next pipeline step (INV-STR-2).
        """
        lost: list[ShardKey] = []
        # Iterate a snapshot: we mutate ``_held`` as we discover losses.
        for shard, lease in list(self._held.items()):
            renewed = await self._renew(
                keys=[shard.lease_key],
                args=[lease.runner_id, self._ttl_ms],
            )
            if int(renewed) != 1:
                lost.append(shard)
                self._held.pop(shard, None)
        return lost

    # -- claimable scan -----------------------------------------------------------

    async def claimable_scan(self, candidates: Iterable[ShardKey]) -> list[ShardKey]:
        """Of ``candidates``, return those with **no live lease** (§8.2, 2 s cadence).

        The runner derives ``candidates`` from
        ``streams.application.desired_state.claimable_desired_states()`` (run-state ∈
        {running, paused} or a converging lifecycle state) fanned out over each
        stream's ``shard_count``. This filters them to shards a runner could claim
        right now — those whose lease key has expired (failover) or never existed
        (first start). Already-held shards are excluded (we own them).
        """
        out: list[ShardKey] = []
        for shard in candidates:
            if shard in self._held:
                continue
            if not bool(await self._redis.exists(shard.lease_key)):
                out.append(shard)
        return out

    # -- release ------------------------------------------------------------------

    async def release(self, stream_id: UUID | str, shard_id: int) -> bool:
        """Compare-owner ``DEL`` the lease (§8.2 release at ``stopped`` finalize).

        Returns ``True`` if we owned and deleted it. A non-owner (or already-expired)
        lease is left untouched — never delete a lease a new holder now owns.
        """
        shard = ShardKey.of(stream_id, shard_id)
        deleted = await self._release(keys=[shard.lease_key], args=[self.runner_id])
        self._held.pop(shard, None)
        return int(deleted) == 1

    async def shutdown(self) -> None:
        """Release every held lease (graceful runner shutdown)."""
        for shard in list(self._held):
            await self.release(shard.stream_id, shard.shard_id)
