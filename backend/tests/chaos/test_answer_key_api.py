"""GET /api/v1/streams/{id}/answer-key/{injections,summary,export}.

Covers api-spec §4.13 / chaos-engine §7.2-7.3: the list/count/export response
shapes, the admin-OR-answer_key:read gate (member-without-scope → 403),
foreign-workspace 404 masking, and the chaos.answer_key.accessed access audit.
"""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.chaos.conftest import ChaosApiWorld, seed_injections

INJ_URL = "/api/v1/streams/{sid}/answer-key/injections"
SUMMARY_URL = "/api/v1/streams/{sid}/answer-key/summary"
EXPORT_URL = "/api/v1/streams/{sid}/answer-key/export"


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
def test_injections_list_shape(api_world: ChaosApiWorld) -> None:
    """Admin lists injection records: {data, next_cursor}, flattened details."""
    seed_injections(api_world, count=3)
    resp = _jwt_client(api_world.admin).get(INJ_URL.format(sid=api_world.stream_id))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert len(body["data"]) == 3
    assert body["next_cursor"] is None  # one page, last page → null (P-2)
    rec = body["data"][0]
    assert rec["mode"] == "duplicates"
    assert rec["copies"] == 1  # details flattened to the top level
    assert "injection_id" in rec and "event_id" in rec


@pytest.mark.django_db
def test_injections_pagination_cursor(api_world: ChaosApiWorld) -> None:
    """The cursor walks every record without overlap; last page → null cursor."""
    seed_injections(api_world, count=5)
    api = _key_client(api_world.answer_key_key)
    url = INJ_URL.format(sid=api_world.stream_id)
    page1 = api.get(url, {"limit": "2"}).json()
    assert len(page1["data"]) == 2
    assert page1["next_cursor"] is not None
    seen = [r["injection_id"] for r in page1["data"]]
    cursor = page1["next_cursor"]
    for _ in range(5):
        page = api.get(url, {"cursor": cursor, "limit": "2"}).json()
        seen.extend(r["injection_id"] for r in page["data"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(set(seen)) == 5


@pytest.mark.django_db
def test_summary_per_mode_counts(api_world: ChaosApiWorld) -> None:
    """Summary returns all seven mode keys with counts (zeros included)."""
    seed_injections(api_world, count=4, mode="duplicates")
    resp = _jwt_client(api_world.admin).get(SUMMARY_URL.format(sid=api_world.stream_id))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["total_injections"] == 4
    assert body["by_mode"]["duplicates"]["injections"] == 4
    assert body["by_mode"]["duplicates"]["extra_copies"] == 4
    assert body["by_mode"]["missing"]["injections"] == 0  # zero included
    assert set(body["by_mode"]) == {
        "missing", "duplicates", "corrupted_values", "nulls",
        "schema_drift", "out_of_order", "late_arriving",
    }


@pytest.mark.django_db
def test_export_jsonl(api_world: ChaosApiWorld) -> None:
    """Export streams JSONL (one record per line, ndjson content type)."""
    import json

    seed_injections(api_world, count=3)
    resp = _key_client(api_world.answer_key_key).get(EXPORT_URL.format(sid=api_world.stream_id))
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"] == "application/x-ndjson"
    streamed = b"".join(resp.streaming_content)  # type: ignore[attr-defined]
    lines = [ln for ln in streamed.decode().splitlines() if ln]
    assert len(lines) == 3
    rec = json.loads(lines[0])
    assert rec["mode"] == "duplicates"


@pytest.mark.django_db
def test_member_without_scope_forbidden(api_world: ChaosApiWorld) -> None:
    """A non-admin member (JWT) and an unscoped key in their own ws → 403 (AK-1)."""
    seed_injections(api_world, count=1)
    member_resp = _jwt_client(api_world.member).get(INJ_URL.format(sid=api_world.stream_id))
    assert member_resp.status_code == 403, member_resp.content

    key_resp = _key_client(api_world.noscope_key).get(INJ_URL.format(sid=api_world.stream_id))
    assert key_resp.status_code == 403, key_resp.content


@pytest.mark.django_db
def test_admin_access_is_audited(api_world: ChaosApiWorld) -> None:
    """Every answer-key read writes chaos.answer_key.accessed (AK-3)."""
    from audit.domain.models import AuditLog

    seed_injections(api_world, count=1)
    _jwt_client(api_world.admin).get(
        INJ_URL.format(sid=api_world.stream_id), {"mode": "duplicates"}
    )
    assert AuditLog.objects.filter(
        action="chaos.answer_key.accessed",
        target_id=api_world.stream_id,
    ).exists()


@pytest.mark.django_db
def test_foreign_workspace_masks_404(api_world: ChaosApiWorld) -> None:
    """A foreign JWT (admin of another workspace) → 404, never 403 (W-3)."""
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    other = User.objects.create_user(email="ak-foreign@example.com", password="pw-correct-horse")
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    tenancy_services.create_workspace(user=other, name="Foreign AK", slug=None)
    ws_context.activate(api_world.workspace.id)

    for url in (INJ_URL, SUMMARY_URL, EXPORT_URL):
        resp = _jwt_client(other).get(url.format(sid=api_world.stream_id))
        assert resp.status_code == 404, (url, resp.content)
