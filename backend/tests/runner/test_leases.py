"""Unit tests for the runner lease/fencing module (backend-architecture §8.2).

Covers the five §8.2 / INV-STR-2 guarantees:

* ``acquire`` succeeds exactly once, then the ``SET NX`` blocks a second runner;
* the fencing token is strictly monotonic per shard across acquire cycles;
* ``heartbeat`` renews held leases and reports the ones lost (TTL expiry / takeover);
* ``release`` is owner-compared (a non-owner cannot delete a live lease);
* the conditional-write helper rejects a stale fencing token (the zombie fence).

Async, against fakeredis (or live Redis via ``DF_TEST_REDIS_URL`` — see conftest).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from runner import leases
from runner.fencing import FencingError, enforce_conditional_write, is_fresh_token
from runner.leases import Lease, LeaseManager, ShardKey, lease_key


def _mgr(client: aioredis.Redis, runner_id: str, **kw: int) -> LeaseManager:
    return LeaseManager(client, runner_id, **kw)


# -- acquire / NX-block -----------------------------------------------------------


async def test_acquire_succeeds_then_nx_blocks_a_second_runner(
    redis_client: aioredis.Redis,
) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A")
    b = _mgr(redis_client, "runner-B")

    lease_a = await a.acquire(stream, shard)
    assert lease_a is not None
    assert lease_a.runner_id == "runner-A"
    assert lease_a.fencing_token >= 1
    # The lease key holds the JSON {runner_id, fencing_token} payload (§8.2).
    raw = await redis_client.get(lease_key(stream, shard))
    assert json.loads(raw) == {
        "runner_id": "runner-A",
        "fencing_token": lease_a.fencing_token,
    }

    # NX blocks the second runner while A's lease is live.
    assert await b.acquire(stream, shard) is None
    assert ShardKey.of(stream, shard) not in b.held
    # A still owns it.
    assert ShardKey.of(stream, shard) in a.held


async def test_acquire_sets_ttl(redis_client: aioredis.Redis) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A", ttl_ms=15_000)
    assert await a.acquire(stream, shard) is not None
    pttl = await redis_client.pttl(lease_key(stream, shard))
    # PX 15000 — allow scheduling slack but assert a real, bounded TTL was set.
    assert 0 < pttl <= 15_000


# -- fencing token monotonicity ---------------------------------------------------


async def test_fencing_token_strictly_monotonic_across_acquire_cycles(
    redis_client: aioredis.Redis,
) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A")
    b = _mgr(redis_client, "runner-B")

    lease1 = await a.acquire(stream, shard)
    assert lease1 is not None
    # A releases (e.g. stop), B re-acquires — its token MUST be strictly greater.
    assert await a.release(stream, shard) is True
    lease2 = await b.acquire(stream, shard)
    assert lease2 is not None
    assert lease2.fencing_token > lease1.fencing_token

    # And even a *failed* acquire (NX-blocked) still burns a strictly-greater token,
    # so a later winner always carries a higher token than any prior holder (§8.2:
    # "strictly monotonic per shard across all time").
    blocked = await a.acquire(stream, shard)  # B holds it now → None
    assert blocked is None
    assert await b.release(stream, shard) is True
    lease3 = await a.acquire(stream, shard)
    assert lease3 is not None
    assert lease3.fencing_token > lease2.fencing_token


async def test_fencing_tokens_independent_per_shard(redis_client: aioredis.Redis) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A")
    l0 = await a.acquire(stream, 0)
    l1 = await a.acquire(stream, 1)
    assert l0 is not None and l1 is not None
    # Each shard has its own fence counter; both start at 1.
    assert l0.fencing_token == 1
    assert l1.fencing_token == 1


# -- heartbeat: renew-all + loss detection ----------------------------------------


async def test_heartbeat_renews_all_held_leases(redis_client: aioredis.Redis) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A", ttl_ms=15_000)
    await a.acquire(stream, 0)
    await a.acquire(stream, 1)

    # Knock the TTLs down, then heartbeat must push them back to the full window.
    await redis_client.pexpire(lease_key(stream, 0), 200)
    await redis_client.pexpire(lease_key(stream, 1), 200)

    lost = await a.heartbeat()
    assert lost == []
    assert await redis_client.pttl(lease_key(stream, 0)) > 10_000
    assert await redis_client.pttl(lease_key(stream, 1)) > 10_000


async def test_heartbeat_detects_lost_lease_on_expiry(redis_client: aioredis.Redis) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A")
    await a.acquire(stream, 0)
    await a.acquire(stream, 1)

    # Simulate shard 0's lease expiring (A paused mid-tick / GC / missed beats).
    await redis_client.delete(lease_key(stream, 0))

    lost = await a.heartbeat()
    assert lost == [ShardKey.of(stream, 0)]
    # The lost shard is dropped from the held set; the live one stays.
    assert ShardKey.of(stream, 0) not in a.held
    assert ShardKey.of(stream, 1) in a.held


async def test_heartbeat_detects_takeover_by_another_runner(
    redis_client: aioredis.Redis,
) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A")
    b = _mgr(redis_client, "runner-B")
    await a.acquire(stream, shard)

    # The lease expires; B claims it (a higher fencing token). A is now a zombie.
    await redis_client.delete(lease_key(stream, shard))
    lease_b = await b.acquire(stream, shard)
    assert lease_b is not None

    # A's renew is compare-owner: it must NOT renew B's lease, and must report loss.
    lost = await a.heartbeat()
    assert lost == [ShardKey.of(stream, shard)]
    # B's lease value is untouched by A's heartbeat.
    raw = await redis_client.get(lease_key(stream, shard))
    assert json.loads(raw)["runner_id"] == "runner-B"
    # B's own heartbeat still renews fine.
    assert await b.heartbeat() == []


# -- release: owner-compared ------------------------------------------------------


async def test_release_is_owner_compared(redis_client: aioredis.Redis) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A")
    b = _mgr(redis_client, "runner-B")
    await a.acquire(stream, shard)

    # A non-owner cannot delete the lease.
    assert await b.release(stream, shard) is False
    assert await redis_client.exists(lease_key(stream, shard)) == 1

    # The owner can.
    assert await a.release(stream, shard) is True
    assert await redis_client.exists(lease_key(stream, shard)) == 0
    assert ShardKey.of(stream, shard) not in a.held


async def test_release_of_missing_lease_is_noop(redis_client: aioredis.Redis) -> None:
    stream, shard = str(uuid4()), 0
    a = _mgr(redis_client, "runner-A")
    assert await a.release(stream, shard) is False


async def test_shutdown_releases_all_held(redis_client: aioredis.Redis) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A")
    await a.acquire(stream, 0)
    await a.acquire(stream, 1)
    await a.shutdown()
    assert await redis_client.exists(lease_key(stream, 0)) == 0
    assert await redis_client.exists(lease_key(stream, 1)) == 0
    assert dict(a.held) == {}


# -- claimable scan ---------------------------------------------------------------


async def test_claimable_scan_returns_shards_without_a_live_lease(
    redis_client: aioredis.Redis,
) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A")
    b = _mgr(redis_client, "runner-B")

    held_by_a = ShardKey.of(stream, 0)
    free_failover = ShardKey.of(stream, 1)
    never_started = ShardKey.of(stream, 2)
    await a.acquire(stream, 0)  # held by A → not claimable by B

    candidates = [held_by_a, free_failover, never_started]
    claimable = await b.claimable_scan(candidates)
    assert free_failover in claimable
    assert never_started in claimable
    assert held_by_a not in claimable


async def test_claimable_scan_excludes_own_held(redis_client: aioredis.Redis) -> None:
    stream = str(uuid4())
    a = _mgr(redis_client, "runner-A")
    await a.acquire(stream, 0)
    # A already owns shard 0, so it is not in A's own claimable set even though the
    # key exists (the in-process held set short-circuits the EXISTS check).
    claimable = await a.claimable_scan([ShardKey.of(stream, 0)])
    assert claimable == []


# -- conditional-write fencing helper (INV-STR-2) ---------------------------------


def test_enforce_conditional_write_accepts_a_landed_write() -> None:
    # rows_affected > 0 → the guarded UPDATE matched (our token won) → no error.
    enforce_conditional_write(
        1, stream_id=str(uuid4()), shard_id=0, my_token=7, surface="checkpoint"
    )


def test_enforce_conditional_write_rejects_a_stale_token() -> None:
    stream = str(uuid4())
    # rows_affected == 0 → the WHERE fencing_token <= mine matched nothing, i.e. a
    # strictly-greater token already won. The stale writer is fenced (INV-STR-2).
    with pytest.raises(FencingError) as exc:
        enforce_conditional_write(
            0, stream_id=stream, shard_id=0, my_token=3, surface="checkpoint"
        )
    assert exc.value.my_token == 3
    assert exc.value.surface == "checkpoint"
    assert str(stream) in str(exc.value)


def test_is_fresh_token_policy_mirrors_the_sql_guard() -> None:
    # WHERE fencing_token <= mine: equal or lower stored token is overwritable.
    assert is_fresh_token(5, None) is True   # no row yet → writable
    assert is_fresh_token(5, 5) is True      # same holder re-checkpointing
    assert is_fresh_token(5, 4) is True      # we advanced past the stored token
    assert is_fresh_token(5, 6) is False     # a newer holder owns the row → fenced


def test_lease_value_is_canonical_json() -> None:
    lease = Lease(shard=ShardKey.of("s", 0), runner_id="r", fencing_token=42)
    assert lease.value == '{"fencing_token":42,"runner_id":"r"}'


def test_lease_and_fence_key_templates() -> None:
    assert leases.lease_key("abc", 3) == "df:lease:abc:3"
    from runner.fencing import fence_key

    assert fence_key("abc", 3) == "df:fence:abc:3"
