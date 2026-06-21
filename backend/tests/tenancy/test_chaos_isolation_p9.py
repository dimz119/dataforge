"""TEN §7.5 (Phase 9) chaos isolation — permanent, unskippable (INV-CHA-7, §9).

Phase-9 exit criterion #6 (PR, permanent):

* **Max-rate chaos never leaks across workspaces.** All seven modes at rate 0.5 on
  workspace B's stream produce injections scoped to B only; workspace A's
  answer-key surfaces (summary + injections) for A's stream show ZERO injections
  attributed to B's policy. Chaos config + effects are strictly per-stream /
  per-workspace (INV-CHA-7) — the scoped managers + RLS on ``chaos_injections``
  enforce it structurally (AK-4).
* **Answer key inaccessible with foreign or under-scoped credentials.** The
  foreign-credential → 404 masking auto-enrolls via the cross-tenant probe (the
  answer-key routes are classified ``SCOPE``); this module pins the chaos-specific
  binding: a foreign admin → 404 on B's stream, and an own member without
  ``answer_key:read`` (and an own unscoped key) → 403 (AK-1).

Data-plane shaped; the auth/scope/ownership gate fires before any row read, so the
suite is Postgres-or-SQLite agnostic on the permanent ``tenancy`` lane.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.tenancy


@dataclass
class _P9World:
    """Workspace A (victim) + workspace B (max-rate chaos) — the P9 isolation world."""

    a_admin: Any
    a_member: Any
    a_stream: str
    a_noscope_key: str
    b_admin: Any
    b_stream: str
    b_seeded: int

INJ_URL = "/api/v1/streams/{sid}/answer-key/injections"
SUMMARY_URL = "/api/v1/streams/{sid}/answer-key/summary"
_ALL_MODES = (
    "missing",
    "duplicates",
    "corrupted_values",
    "nulls",
    "schema_drift",
    "out_of_order",
    "late_arriving",
)


def _jwt_client(user: Any) -> APIClient:
    from identity.infra.jwt import issue_token_pair

    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(user).access_token}")
    return api


def _key_client(key: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_API_KEY=key)
    return api


def _make_stream(workspace: Any, admin: Any, name: str) -> str:
    from streams.domain.models import Stream

    stream_id = str(uuid.uuid4())
    Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name=name,
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=datetime.now(UTC),
    )
    return stream_id


def _seed_max_rate_chaos(workspace: Any, stream_id: str) -> int:
    """Insert one injection per mode (the all-7-at-0.5 answer-key footprint) for B."""
    from chaos.domain.models import ChaosInjection
    from tenancy.application.services import worker_workspace_scope

    base = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)
    rows = [
        ChaosInjection(
            injection_id=uuid.uuid4(),
            workspace_id=workspace.id,
            stream_id=uuid.UUID(stream_id),
            shard_id=0,
            mode=mode,
            event_id=uuid.uuid4(),
            sequence_no=i,
            occurred_at=base + timedelta(seconds=i),
            canonical_emitted_at=base + timedelta(seconds=i),
            details={},
            recorded_at=base + timedelta(seconds=i),
        )
        for i, mode in enumerate(_ALL_MODES)
    ]
    with worker_workspace_scope(workspace.id):
        ChaosInjection.objects.bulk_create(rows)
    return len(rows)


@pytest.fixture
def p9_world(db: Any) -> _P9World:
    """Workspace A (victim, own admin + clean stream) and workspace B (max-rate chaos)."""
    from identity.domain.models import User
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN, ROLE_MEMBER

    a_admin = User.objects.create_user(email="p9-a@example.com", password="pw-correct-horse")
    a_admin.is_verified = True
    a_admin.save(update_fields=["is_verified"])
    ws_a = tenancy_services.create_workspace(user=a_admin, name="P9 A", slug=None)
    ws_context.activate(ws_a.id)
    a_member = User.objects.create_user(email="p9-a-mem@example.com", password="pw-correct-horse")
    a_member.is_verified = True
    a_member.save(update_fields=["is_verified"])
    tenancy_services.add_member(
        workspace=ws_a, email=a_member.email, role=ROLE_MEMBER, actor=a_admin
    )
    a_stream = _make_stream(ws_a, a_admin, "p9-a-stream")
    with ws_context.workspace_context(ws_a.id):
        _f, a_noscope_key = key_service.create_key(
            workspace=ws_a, actor=a_admin, name="a-noscope",
            scopes=["events:read"], expires_at=None, actor_role=ROLE_ADMIN,
        )

    b_admin = User.objects.create_user(email="p9-b@example.com", password="pw-correct-horse")
    b_admin.is_verified = True
    b_admin.save(update_fields=["is_verified"])
    ws_b = tenancy_services.create_workspace(user=b_admin, name="P9 B", slug=None)
    b_stream = _make_stream(ws_b, b_admin, "p9-b-stream")
    seeded = _seed_max_rate_chaos(ws_b, b_stream)
    ws_context.activate(ws_a.id)

    return _P9World(
        a_admin=a_admin,
        a_member=a_member,
        a_stream=a_stream,
        a_noscope_key=a_noscope_key,
        b_admin=b_admin,
        b_stream=b_stream,
        b_seeded=seeded,
    )


@pytest.mark.django_db
def test_p9_max_rate_chaos_does_not_leak_into_workspace_a(p9_world: _P9World) -> None:
    """A's answer key shows ZERO injections — B's max-rate chaos never crosses (INV-CHA-7)."""
    resp = _jwt_client(p9_world.a_admin).get(SUMMARY_URL.format(sid=p9_world.a_stream))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["total_injections"] == 0
    assert all(body["by_mode"][m]["injections"] == 0 for m in _ALL_MODES)
    inj = _jwt_client(p9_world.a_admin).get(INJ_URL.format(sid=p9_world.a_stream))
    assert inj.status_code == 200
    assert inj.json()["data"] == []


@pytest.mark.django_db
def test_p9_b_admin_sees_its_own_max_rate_chaos(p9_world: _P9World) -> None:
    """Control: B's own admin sees all seven modes — the isolation is a boundary, not a deny."""
    resp = _jwt_client(p9_world.b_admin).get(SUMMARY_URL.format(sid=p9_world.b_stream))
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["total_injections"] == p9_world.b_seeded
    assert all(body["by_mode"][m]["injections"] == 1 for m in _ALL_MODES)


@pytest.mark.django_db
def test_p9_foreign_admin_answer_key_is_404(p9_world: _P9World) -> None:
    """A's admin (foreign to B) → 404 on B's answer key (never 403; W-3 masking)."""
    for url in (SUMMARY_URL, INJ_URL):
        resp = _jwt_client(p9_world.a_admin).get(url.format(sid=p9_world.b_stream))
        assert resp.status_code == 404, (url, resp.content)


@pytest.mark.django_db
def test_p9_under_scoped_own_credentials_forbidden(p9_world: _P9World) -> None:
    """An own member + an own unscoped key → 403 on the answer key (AK-1)."""
    member_resp = _jwt_client(p9_world.a_member).get(INJ_URL.format(sid=p9_world.a_stream))
    assert member_resp.status_code == 403, member_resp.content
    key_resp = _key_client(p9_world.a_noscope_key).get(INJ_URL.format(sid=p9_world.a_stream))
    assert key_resp.status_code == 403, key_resp.content
