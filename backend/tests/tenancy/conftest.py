"""Fixtures for the permanent TEN cross-tenant attack suite (testing §7).

Re-exports the base two-workspace factory from ``tenancy.tests.conftest`` (so the
raw-SQL RLS probes keep working) and adds the richer ``victim`` / ``attacker``
``Tenant`` fixtures the OpenAPI route-probe suite parametrizes over: each carries
a verified admin, a console JWT, and a live API key with planted sentinels
(§7.1).

The Redis revocation cache is replaced with an in-memory fake so the PR lane runs
on SQLite without live Redis; the production Redis path is exercised by the OPS
revocation stopwatch and the compose demo.
"""

from __future__ import annotations

from typing import Any

import pytest

from tenancy.infra import revocation_cache
from tenancy.tests.conftest import (  # noqa: F401  (re-exported as fixtures)
    make_user,
    make_workspace,
    password,
)
from tests.tenancy.ten_fixture import Tenant, build_tenant


@pytest.fixture(autouse=True)
def _fake_revocation_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-memory revocation cache: TEN runs without live Redis (testing §17.1)."""
    store: dict[str, Any] = {}
    revoked = revocation_cache.STATE_REVOKED
    monkeypatch.setattr(revocation_cache, "get_state", store.get)
    monkeypatch.setattr(revocation_cache, "put_active", lambda p, s: store.__setitem__(p, s))
    monkeypatch.setattr(revocation_cache, "put_revoked", lambda p: store.__setitem__(p, revoked))
    monkeypatch.setattr(revocation_cache, "touch_last_used", lambda _id: None)


@pytest.fixture
def victim(make_user) -> Tenant:  # type: ignore[no-untyped-def]
    """Workspace A — the victim tenant whose resources B attacks (§7.1)."""
    return build_tenant(make_user=make_user, label="A")


@pytest.fixture
def attacker(make_user) -> Tenant:  # type: ignore[no-untyped-def]
    """Workspace B — the attacker tenant with valid-but-foreign credentials."""
    return build_tenant(make_user=make_user, label="B")
