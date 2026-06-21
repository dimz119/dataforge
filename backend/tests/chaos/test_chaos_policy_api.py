"""PATCH | GET /api/v1/streams/{id}/chaos — the live ChaosPolicy surface.

Covers api-spec §4.8.3 / chaos-engine §3.4-3.5: the rate ≤ 0.5 validation gate
(422), the desired-state ``chaos_config`` write, the
``streams.stream.chaos_policy_changed`` audit, scope gating (streams:write), and
foreign-workspace 404 masking.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.chaos.conftest import ChaosApiWorld

CHAOS_URL = "/api/v1/streams/{sid}/chaos"


def _jwt_client(user: Any) -> APIClient:
    from identity.infra.jwt import issue_token_pair

    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(user).access_token}")
    return api


def _key_client(key: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_API_KEY=key)
    return api


@pytest.mark.django_db
def test_patch_rejects_rate_above_half(api_world: ChaosApiWorld) -> None:
    """A mode rate > 0.5 → 422 manifest-validation-failed with the CH-V01 error."""
    api = _jwt_client(api_world.admin)
    resp = api.patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"duplicates": {"enabled": True, "rate": 0.9, "params": {}}},
        format="json",
    )
    assert resp.status_code == 422, resp.content
    body = resp.json()
    assert body["status"] == 422
    codes = [e["code"] for e in body["errors"]]
    assert "CH-V01" in codes
    # The rejected config was NOT written.
    api_world.stream.refresh_from_db()
    assert "duplicates" not in api_world.stream.chaos_config


@pytest.mark.django_db
def test_patch_writes_config_and_audits(api_world: ChaosApiWorld) -> None:
    """A valid PATCH writes chaos_config + audits streams.stream.chaos_policy_changed."""
    from audit.domain.models import AuditLog

    api = _jwt_client(api_world.admin)
    resp = api.patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"late_arriving": {"enabled": True, "rate": 0.05, "params": {}}},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    # Response is the closed seven-mode policy with the change applied.
    assert body["modes"]["late_arriving"]["enabled"] is True
    assert body["modes"]["late_arriving"]["rate"] == 0.05
    assert body["modes"]["missing"]["enabled"] is False  # untouched default

    api_world.stream.refresh_from_db()
    assert api_world.stream.chaos_config["late_arriving"]["rate"] == 0.05

    assert AuditLog.objects.filter(
        action="streams.stream.chaos_policy_changed",
        target_id=api_world.stream_id,
    ).exists()


@pytest.mark.django_db
def test_patch_rejects_unknown_mode_key(api_world: ChaosApiWorld) -> None:
    """An unknown top-level key → 422 CH-V09 (closed shape)."""
    api = _jwt_client(api_world.admin)
    resp = api.patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"not_a_mode": {"enabled": True, "rate": 0.1}},
        format="json",
    )
    assert resp.status_code == 422, resp.content
    assert "CH-V09" in [e["code"] for e in resp.json()["errors"]]


@pytest.mark.django_db
def test_patch_api_key_requires_streams_write(api_world: ChaosApiWorld) -> None:
    """A key with streams:write succeeds; a key without it → 403 within own ws."""
    ok = _key_client(api_world.streams_write_key).patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"missing": {"enabled": True, "rate": 0.02, "params": {}}},
        format="json",
    )
    assert ok.status_code == 200, ok.content

    denied = _key_client(api_world.streams_read_key).patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"missing": {"enabled": True, "rate": 0.02, "params": {}}},
        format="json",
    )
    assert denied.status_code == 403, denied.content


@pytest.mark.django_db
def test_get_returns_live_policy(api_world: ChaosApiWorld) -> None:
    """GET returns the live policy (defaults when nothing has been written)."""
    api = _key_client(api_world.streams_read_key)
    resp = api.get(CHAOS_URL.format(sid=api_world.stream_id))
    assert resp.status_code == 200, resp.content
    modes = resp.json()["modes"]
    assert {
        "missing", "duplicates", "corrupted_values", "nulls", "schema_drift",
        "out_of_order", "late_arriving", "on_stop_policy",
    } <= set(modes)


@pytest.mark.django_db
def test_foreign_workspace_patch_masks_404(api_world: ChaosApiWorld) -> None:
    """A valid JWT from another workspace → 404 (never 403/2xx — W-3)."""
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    other = User.objects.create_user(email="chaos-foreign@example.com", password="pw-correct-horse")
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    tenancy_services.create_workspace(user=other, name="Foreign", slug=None)
    ws_context.activate(api_world.workspace.id)

    resp = _jwt_client(other).patch(
        CHAOS_URL.format(sid=api_world.stream_id),
        {"missing": {"enabled": True, "rate": 0.02, "params": {}}},
        format="json",
    )
    assert resp.status_code == 404, resp.content
