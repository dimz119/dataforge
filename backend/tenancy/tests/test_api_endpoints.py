"""Tenancy HTTP endpoints + the §3.3 401/403/404 policy.

The cross-tenant behaviour the TEN attack suite generalises: a foreign-workspace
object returns 404 (never 403 — existence is never confirmed); insufficient role
within an accessible workspace returns 403 with ``required_role``; both auth
headers present → 400 ambiguous-credentials.
"""

from __future__ import annotations

from typing import Any

import pytest

from tenancy.infra import revocation_cache
from tenancy.tests.conftest import auth

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fake_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise Redis in endpoint tests (no live Redis in CI unit lane)."""
    store: dict[str, Any] = {}
    revoked = revocation_cache.STATE_REVOKED
    monkeypatch.setattr(revocation_cache, "get_state", lambda p: store.get(p))
    monkeypatch.setattr(revocation_cache, "put_active", lambda p, s: store.__setitem__(p, s))
    monkeypatch.setattr(revocation_cache, "put_revoked", lambda p: store.__setitem__(p, revoked))
    monkeypatch.setattr(revocation_cache, "touch_last_used", lambda _id: None)


def test_create_workspace_endpoint(api, make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user("create-ep@example.com", is_verified=True)
    resp = auth(api, user).post(
        "/api/v1/workspaces", {"name": "My Lab"}, format="json"
    )
    assert resp.status_code == 201
    assert resp.data["role"] == "admin"
    assert resp.data["plan"] == "free"
    assert resp.data["member_count"] == 1


def test_unverified_create_workspace_403_email_not_verified(api, make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user("unv-ep@example.com", is_verified=False)
    resp = auth(api, user).post("/api/v1/workspaces", {"name": "X"}, format="json")
    assert resp.status_code == 403
    assert resp.data["type"].endswith("/email-not-verified")


def test_foreign_workspace_returns_404_not_403(api, workspace_a, workspace_b) -> None:  # type: ignore[no-untyped-def]
    """B requesting A's workspace → 404 (W-3 masking), never 403 (demo step 9)."""
    resp = auth(api, workspace_b.admin).get(f"/api/v1/workspaces/{workspace_a.workspace.id}")
    assert resp.status_code == 404
    assert resp.data["type"].endswith("/not-found")


def test_member_listing_own_workspace(api, workspace_a) -> None:  # type: ignore[no-untyped-def]
    resp = auth(api, workspace_a.admin).get(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/members"
    )
    assert resp.status_code == 200
    assert len(resp.data["data"]) == 1


def test_non_admin_member_add_forbidden_with_required_role(api, workspace_a, make_user) -> None:  # type: ignore[no-untyped-def]
    """A member (non-admin) adding members → 403 with required_role=admin."""
    from tenancy.application import services
    from tenancy.domain.context import workspace_context

    member = make_user("plain-member@example.com", is_verified=True)
    with workspace_context(workspace_a.workspace.id):
        services.add_member(
            workspace=workspace_a.workspace,
            email=member.email,
            role="member",
            actor=workspace_a.admin,
        )
    resp = auth(api, member).post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/members",
        {"email": "x@example.com", "role": "member"},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.data["type"].endswith("/permission-denied")
    assert resp.data["required_role"] == "admin"


def test_api_key_create_reveal_once_then_list_hides_secret(api, workspace_a) -> None:  # type: ignore[no-untyped-def]
    client = auth(api, workspace_a.admin)
    create = client.post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys",
        {"name": "demo", "scopes": ["events:read", "streams:write"]},
        format="json",
    )
    assert create.status_code == 201
    assert create.data["key"].startswith("df_")  # reveal-once
    assert "key" in create.data

    listing = client.get(f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys")
    assert listing.status_code == 200
    item = listing.data["data"][0]
    assert "key" not in item  # the plaintext never reappears
    assert item["state"] == "active"
    assert "prefix" in item and "last4" in item


def test_both_credentials_present_400_ambiguous(api, workspace_a) -> None:  # type: ignore[no-untyped-def]
    """Authorization + X-API-Key on one request → 400 ambiguous-credentials (A-2)."""
    create = auth(api, workspace_a.admin).post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys",
        {"name": "amb", "scopes": ["events:read"]},
        format="json",
    )
    key = create.data["key"]
    # Quota view accepts both JWT and Key; send both → 400.
    client = auth(api, workspace_a.admin)
    resp = client.get(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/quotas",
        HTTP_X_API_KEY=key,
    )
    assert resp.status_code == 400
    assert resp.data["type"].endswith("/ambiguous-credentials")


def test_key_info_probe_returns_workspace_then_401_after_revoke(api, workspace_a) -> None:  # type: ignore[no-untyped-def]
    """key-info returns the workspace for a live key, 401 after revoke (demo 7-8)."""
    client = auth(api, workspace_a.admin)
    create = client.post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys",
        {"name": "probe", "scopes": ["events:read"]},
        format="json",
    )
    key = create.data["key"]
    key_id = create.data["api_key_id"]

    # Fresh client (no Authorization) — key-info is the API-key-only probe.
    probe_client = type(api)()
    probe = probe_client.get("/api/v1/auth/key-info", HTTP_X_API_KEY=key)
    assert probe.status_code == 200
    assert probe.data["workspace_id"] == str(workspace_a.workspace.id)

    revoke = client.delete(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys/{key_id}"
    )
    assert revoke.status_code == 204
    after = type(api)().get("/api/v1/auth/key-info", HTTP_X_API_KEY=key)
    assert after.status_code == 401
    assert after.data["type"].endswith("/invalid-api-key")


def test_key_on_jwt_only_surface_is_absent_credential_401(api, workspace_a) -> None:  # type: ignore[no-untyped-def]
    """An API key on the JWT-only workspaces surface → 401 (SEC-AUTH-1)."""
    create = auth(api, workspace_a.admin).post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys",
        {"name": "wrong-surface", "scopes": ["streams:read"]},
        format="json",
    )
    key = create.data["key"]
    fresh = type(api)()  # no Authorization
    resp = fresh.get(
        f"/api/v1/workspaces/{workspace_a.workspace.id}", HTTP_X_API_KEY=key
    )
    assert resp.status_code == 401


def test_foreign_key_revoke_404(api, workspace_a, workspace_b) -> None:  # type: ignore[no-untyped-def]
    """B trying to revoke A's key id → 404 (foreign object masked)."""
    create = auth(api, workspace_a.admin).post(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys",
        {"name": "a-key", "scopes": ["events:read"]},
        format="json",
    )
    a_key_id = create.data["api_key_id"]
    # B references A's workspace → resolves to 404 before the key id even matters.
    resp = auth(api, workspace_b.admin).delete(
        f"/api/v1/workspaces/{workspace_a.workspace.id}/api-keys/{a_key_id}"
    )
    assert resp.status_code == 404
