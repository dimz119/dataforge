"""OPS-6 — revoked API key rejected within 1 s (Phase 2 exit criterion #4).

The contract (SEC-KEY-5): ``revoke_key`` writes the ``revoked`` marker to Redis
**synchronously, before** the 204 response, so the key is rejected platform-wide
in under a second — the revocation cache is consulted first on every verify, ahead
of the DB. This is asserted two ways:

* ``test_revocation_rejected_within_1s`` (PR lane, in-memory cache fake) — proves
  the *logic*: a key minted + verified successfully then revoked is rejected with
  401 ``invalid-api-key`` on the very next verify, with a wall-clock stopwatch on
  the revoke→reject window.
* ``test_revocation_rejected_within_1s_live_redis`` (merge/compose lane) —
  proves it against a **live Redis** (the production path), skipping cleanly when
  Redis is unreachable so the PR lane stays hermetic.

Both go through ``key-info`` (the data-plane probe) exactly as demo step 8 does.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from config.problems import InvalidApiKey
from tenancy.application import keys as key_service
from tenancy.domain.context import workspace_context
from tenancy.domain.models import KEY_SCOPES, ROLE_ADMIN
from tenancy.infra import revocation_cache

pytestmark = [pytest.mark.ops, pytest.mark.django_db]

_BUDGET_SECONDS = 1.0


def _mint(setup: Any) -> tuple[Any, str]:
    with workspace_context(setup.workspace.id):
        return key_service.create_key(
            workspace=setup.workspace,
            actor=setup.admin,
            name="ops-revocation",
            scopes=list(KEY_SCOPES),
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )


class _FakeCache:
    """In-memory revocation cache with the same synchronous-revoke contract."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def get_state(self, prefix: str) -> Any:
        return self.store.get(prefix)

    def put_active(self, prefix: str, state: Any) -> None:
        self.store[prefix] = state

    def put_revoked(self, prefix: str) -> None:
        self.store[prefix] = revocation_cache.STATE_REVOKED

    def touch_last_used(self, _id: Any) -> None:
        return None


def test_revocation_rejected_within_1s(make_workspace, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Revoke → next verify is 401 within 1 s (logic; in-memory cache)."""
    cache = _FakeCache()
    monkeypatch.setattr(revocation_cache, "get_state", cache.get_state)
    monkeypatch.setattr(revocation_cache, "put_active", cache.put_active)
    monkeypatch.setattr(revocation_cache, "put_revoked", cache.put_revoked)
    monkeypatch.setattr(revocation_cache, "touch_last_used", cache.touch_last_used)

    a = make_workspace("ops-rev@example.com")
    api_key, plaintext = _mint(a)

    # Live before revoke (warms the active cache entry).
    verified = key_service.verify_key(plaintext)
    assert verified.api_key_id == api_key.id

    with workspace_context(a.workspace.id):
        start = time.monotonic()
        key_service.revoke_key(
            workspace=a.workspace,
            api_key_id=api_key.id,
            actor=a.admin,
            actor_role=ROLE_ADMIN,
        )
        # The very next verify must reject; measure the revoke→reject window.
        with pytest.raises(InvalidApiKey):
            key_service.verify_key(plaintext)
        elapsed = time.monotonic() - start

    assert elapsed < _BUDGET_SECONDS, f"revocation took {elapsed:.3f}s (> {_BUDGET_SECONDS}s)"


def _redis_available() -> bool:
    try:
        import redis as _redis
        from django.conf import settings

        client = _redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5)
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="OPS-6 live path requires a reachable Redis.")
def test_revocation_rejected_within_1s_live_redis(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """Revoke → next verify is 401 within 1 s against LIVE Redis (production path).

    Runs in the merge/compose lane (testing §2 OPS); the synchronous
    ``put_revoked`` before the 204 is the production guarantee under test.
    """
    a = make_workspace("ops-rev-live@example.com")
    api_key, plaintext = _mint(a)

    assert key_service.verify_key(plaintext).api_key_id == api_key.id

    with workspace_context(a.workspace.id):
        start = time.monotonic()
        key_service.revoke_key(
            workspace=a.workspace,
            api_key_id=api_key.id,
            actor=a.admin,
            actor_role=ROLE_ADMIN,
        )
        with pytest.raises(InvalidApiKey):
            key_service.verify_key(plaintext)
        elapsed = time.monotonic() - start

    assert elapsed < _BUDGET_SECONDS, f"live revocation took {elapsed:.3f}s (> {_BUDGET_SECONDS}s)"
