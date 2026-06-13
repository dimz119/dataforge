"""API-key lifecycle: create (reveal-once), verify, revoke + the < 1 s contract.

The Redis revocation cache is replaced with an in-memory fake so the suite runs
without a live Redis (the production path is exercised in the compose demo). The
fake honours the same contract: synchronous revoke write, active TTL, fail-to-DB
on miss.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from config.problems import InvalidApiKey, PermissionDeniedError
from tenancy.application import keys as key_service
from tenancy.domain.context import workspace_context
from tenancy.domain.models import SCOPE_ANSWER_KEY_READ, ApiKey
from tenancy.infra import keys as key_crypto
from tenancy.infra import revocation_cache

pytestmark = pytest.mark.django_db


class FakeCache:
    """In-memory stand-in for the Redis revocation cache (same contract)."""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.last_used: list[str] = []

    def get_state(self, prefix: str) -> Any:
        return self.store.get(prefix)

    def put_active(self, prefix: str, state: Any) -> None:
        self.store[prefix] = state

    def put_revoked(self, prefix: str) -> None:
        self.store[prefix] = revocation_cache.STATE_REVOKED

    def touch_last_used(self, api_key_id: Any) -> None:
        self.last_used.append(str(api_key_id))


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch) -> FakeCache:
    cache = FakeCache()
    monkeypatch.setattr(revocation_cache, "get_state", cache.get_state)
    monkeypatch.setattr(revocation_cache, "put_active", cache.put_active)
    monkeypatch.setattr(revocation_cache, "put_revoked", cache.put_revoked)
    monkeypatch.setattr(revocation_cache, "touch_last_used", cache.touch_last_used)
    return cache


def _create(setup: Any, scopes: list[str], *, role: str = "admin") -> tuple[ApiKey, str]:
    with workspace_context(setup.workspace.id):
        return key_service.create_key(
            workspace=setup.workspace,
            actor=setup.admin,
            name="test-key",
            scopes=scopes,
            expires_at=None,
            actor_role=role,
        )


def test_create_key_reveals_plaintext_once_and_stores_only_hash(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("k1@example.com")
    api_key, plaintext = _create(setup, ["events:read", "streams:read"])
    # Plaintext is df_<env>_<prefix>_<secret>; only the hash is stored.
    assert plaintext.startswith("df_")
    assert api_key.key_hash == key_crypto.hash_key(plaintext)
    assert api_key.key_hash != plaintext
    assert api_key.last4 == plaintext[-4:]
    assert api_key.key_prefix in plaintext


def test_verify_key_roundtrip(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("k2@example.com")
    api_key, plaintext = _create(setup, ["events:read"])
    verified = key_service.verify_key(plaintext)
    assert verified.api_key_id == api_key.id
    assert verified.workspace_id == setup.workspace.id
    assert "events:read" in verified.scopes


def test_unknown_key_is_invalid(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    make_workspace("k3@example.com")
    with pytest.raises(InvalidApiKey):
        key_service.verify_key("df_dev_aaaaaaaa_" + "x" * 30)


def test_wrong_env_token_rejected(make_workspace, fake_cache, settings) -> None:  # type: ignore[no-untyped-def]
    """SEC-KEY-2: a df_dev_* key fails when the server env token is 'live'."""
    setup = make_workspace("k4@example.com")
    _api_key, plaintext = _create(setup, ["events:read"])
    # plaintext was minted as df_dev_* (test DF_ENV=dev). Flip server to prod.
    settings.DF_ENV = "prod"  # env_token() now returns 'live'
    with pytest.raises(InvalidApiKey):
        key_service.verify_key(plaintext)


def test_revoke_then_verify_fails_within_one_second(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    """SEC-KEY-5: a revoked key is rejected < 1 s via the synchronous cache write."""
    setup = make_workspace("k5@example.com")
    api_key, plaintext = _create(setup, ["events:read"])
    assert key_service.verify_key(plaintext).api_key_id == api_key.id

    started = time.monotonic()
    with workspace_context(setup.workspace.id):
        key_service.revoke_key(
            workspace=setup.workspace,
            api_key_id=api_key.id,
            actor=setup.admin,
            actor_role="admin",
        )
    # The synchronous Redis (fake) revoke write precedes the return; verify now.
    with pytest.raises(InvalidApiKey):
        key_service.verify_key(plaintext)
    assert time.monotonic() - started < 1.0  # well under the 1 s exit criterion


def test_revoked_state_in_cache_short_circuits_verify(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("k6@example.com")
    _api_key, plaintext = _create(setup, ["events:read"])
    parsed = key_crypto.parse_key(plaintext)
    assert parsed is not None
    fake_cache.put_revoked(parsed.key_prefix)
    with pytest.raises(InvalidApiKey):
        key_service.verify_key(plaintext)


def test_revoke_is_idempotent(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("k7@example.com")
    api_key, _plaintext = _create(setup, ["events:read"])
    with workspace_context(setup.workspace.id):
        key_service.revoke_key(
            workspace=setup.workspace, api_key_id=api_key.id, actor=setup.admin, actor_role="admin"
        )
        # A second revoke is a no-op (returns without error → 204 idempotent delete).
        key_service.revoke_key(
            workspace=setup.workspace, api_key_id=api_key.id, actor=setup.admin, actor_role="admin"
        )


def test_answer_key_scope_requires_admin(make_workspace, make_user, fake_cache) -> None:  # type: ignore[no-untyped-def]
    """A non-admin member cannot self-grant answer_key:read (api-spec A-4)."""
    setup = make_workspace("k8@example.com")
    member = make_user("member-key@example.com", is_verified=True)
    with workspace_context(setup.workspace.id):
        from tenancy.application import services

        services.add_member(
            workspace=setup.workspace, email=member.email, role="member", actor=setup.admin
        )
        with pytest.raises(PermissionDeniedError):
            key_service.create_key(
                workspace=setup.workspace,
                actor=member,
                name="forbidden",
                scopes=[SCOPE_ANSWER_KEY_READ],
                expires_at=None,
                actor_role="member",
            )


def test_key_count_quota_enforced(make_workspace, fake_cache) -> None:  # type: ignore[no-untyped-def]
    """Free-tier max_api_keys (5) enforced at command time (INV-TEN-5)."""
    from rest_framework.exceptions import APIException

    setup = make_workspace("k9@example.com")
    for _ in range(5):
        _create(setup, ["events:read"])
    with pytest.raises(APIException) as exc_info:  # quota-exceeded
        _create(setup, ["events:read"])
    assert getattr(exc_info.value, "slug", "") == "quota-exceeded"
