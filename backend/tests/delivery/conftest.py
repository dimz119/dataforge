"""Fixtures for the delivery-plane tests (delivery-channels §3-§4).

The buffer-writer writes to ``event_buffer`` through the Django ``default``
connection (the ``dataforge_app`` NOBYPASSRLS role at runtime); the caller arms the
per-batch workspace context (Layer-1 contextvar + Layer-2 ``app.workspace_id`` GUC)
before delivery so the rows pass RLS (SINK-7). These fixtures arm the same context
the production entrypoint (``runner.sinks.run._arm_tenant``) arms, keyed to the
shared engine test workspace (``dataforge_engine.envelope.tests.fixtures``).

The SQLite unit lane (default ``config.settings.test``) has no RLS — the migration
falls back to a plain ``event_buffer`` table — so these tests run hermetically with
no broker, no Postgres, and no real workspace row (``event_buffer`` carries no FK,
C-7). The Postgres-backed RLS / COPY assertions live under
``tests/delivery/test_postgres`` for the verify agent's lanes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from dataforge_engine.envelope.tests.fixtures import STREAM_ID, WORKSPACE_ID


@pytest.fixture
def armed_workspace(db: Any) -> Iterator[str]:
    """Arm the shared engine-fixture workspace for direct buffer-writer calls.

    Mirrors how ``runner.sinks.run._arm_tenant`` arms each batch's workspace before
    ``deliver``: the scoped-manager contextvar + the ``app.workspace_id`` GUC, both
    inside one transaction (the GUC is ``SET LOCAL``). Yields the workspace id so a
    test can assert on it; cleared on exit.
    """
    import uuid

    from tenancy.application.services import worker_workspace_scope

    with worker_workspace_scope(uuid.UUID(WORKSPACE_ID)):
        yield WORKSPACE_ID


@pytest.fixture
def stream_id() -> str:
    """The shared engine-fixture stream id (one stream, one writer, BW-7)."""
    return STREAM_ID


@pytest.fixture(autouse=True)
def fake_stats_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the StreamStats counters at an in-process fakeredis (SQLite PR lane).

    Autouse so the buffer-writer's counting path (now part of every ``deliver``) is
    hermetic — no test reaches for live Redis. Patches
    ``delivery.infra.stream_stats._redis`` to return one shared ``FakeStrictRedis`` so
    the counting path and the read path share the same in-memory store (mirrors the
    runner conftest's fakeredis). Returns the client so a test can inspect the keys
    directly. The live lanes (``DF_TEST_REDIS_URL``) run the real client unchanged.
    """
    import fakeredis

    from delivery.infra import stream_stats

    client = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(stream_stats, "_redis", lambda: client)
    return client


# -- WebSocket tail consumer fixtures (delivery-channels §6) -------------------
# Re-export the base user/password factory so the WS handshake tests can build a
# real workspace + key + stream; fake the Redis revocation cache so the SQLite PR
# lane runs without live Redis (mirrors tests/tenancy/conftest).
from tenancy.tests.conftest import make_user, password  # noqa: E402, F401


@pytest.fixture
def ws_revocation_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory revocation cache for the WS suite (SQLite PR lane, no live Redis).

    Returns the backing store so a test can plant a revocation
    (``store[prefix] = revocation_cache.STATE_REVOKED``) and assert the < 1 s live
    disconnect (WS-3).
    """
    from tenancy.infra import revocation_cache

    store: dict[str, Any] = {}
    revoked = revocation_cache.STATE_REVOKED
    monkeypatch.setattr(revocation_cache, "get_state", store.get)
    monkeypatch.setattr(revocation_cache, "put_active", lambda p, s: store.__setitem__(p, s))
    monkeypatch.setattr(revocation_cache, "put_revoked", lambda p: store.__setitem__(p, revoked))
    monkeypatch.setattr(revocation_cache, "touch_last_used", lambda _id: None)
    return store


@pytest.fixture
def ws_world(db: Any, make_user: Any, ws_revocation_store: dict[str, Any]) -> Any:
    """A workspace + admin + events:read key + stream for the WS handshake tests."""
    from tests.delivery.ws_fixtures import build_ws_world

    return build_ws_world(make_user=make_user)
