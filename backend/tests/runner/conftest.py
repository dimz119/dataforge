"""Fixtures for the runner lease/fencing unit (backend-architecture §8.2).

The lease unit speaks only to Redis (no Django ORM), so these tests run against an
in-process ``fakeredis`` server by default — fast, hermetic, no service dependency,
and it supports both ``redis.asyncio`` and the server-side Lua (``eval``) the
heartbeat/release scripts need.

A *live-Redis* variant for the verify agent: set ``DF_TEST_REDIS_URL`` to a real
Redis (e.g. the compose ``redis://localhost:6379/15``) and these same tests run
against it (``--redis-flushdb`` between tests keyed off that env). The fakeredis
default keeps the PR lane green without a Redis service; the env override exercises
real server-side Lua semantics in the compose demo (the §8.5 failover/kill-test
OPS suite is compose-only — see the Phase-5 CI note).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio

try:  # fakeredis is a dev dependency; live-Redis runs without it.
    import fakeredis.aioredis as _fakeredis
except ImportError:  # pragma: no cover - exercised only in the live-Redis lane
    _fakeredis = None  # type: ignore[assignment]

import redis.asyncio as aioredis

_LIVE_REDIS_URL = os.environ.get("DF_TEST_REDIS_URL")


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    """A clean async Redis: live (``DF_TEST_REDIS_URL``) or in-process fakeredis.

    Flushes the keyspace before and after each test so fence counters and lease
    keys never leak across tests (the fence ``INCR`` is monotonic *within* a shard's
    lifetime, but each test starts from a pristine server).
    """
    client: aioredis.Redis
    if _LIVE_REDIS_URL:
        client = aioredis.Redis.from_url(_LIVE_REDIS_URL, decode_responses=True)
    else:
        assert _fakeredis is not None, "install fakeredis or set DF_TEST_REDIS_URL"
        client = _fakeredis.FakeRedis(decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
