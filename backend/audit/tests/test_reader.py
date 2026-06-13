"""Tests for the admin-readable audit query path (INV-AUD-4, §10.4)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from audit.application.reader import read_workspace_audit_log
from audit.application.writer import record_audit
from audit.tests.conftest import UserFactory

pytestmark = pytest.mark.django_db


# --- INV-AUD-4 / §10.4: workspace-scoped, account-level rows excluded ---------
def test_read_returns_only_the_requested_workspaces_rows(make_user: UserFactory) -> None:
    user = make_user()
    ws_a, ws_b = uuid4(), uuid4()
    record_audit(action="tenancy.workspace.created", actor=user, workspace_id=ws_a)
    record_audit(action="tenancy.api_key.created", actor=user, workspace_id=ws_b)

    rows = read_workspace_audit_log(workspace_id=ws_a)
    assert [r["action"] for r in rows] == ["tenancy.workspace.created"]
    assert all(r["workspace_id"] == ws_a for r in rows)


def test_account_level_rows_are_never_served(make_user: UserFactory) -> None:
    """Account-level (NULL-workspace) entries are excluded from the read (§10.4)."""
    user = make_user()
    ws = uuid4()
    record_audit(action="tenancy.workspace.created", actor=user, workspace_id=ws)
    # Account-level entries (no workspace_id) — e.g. signup, password change.
    record_audit(action="identity.user.registered", actor=user)
    record_audit(action="identity.user.password_changed", actor=user)

    rows = read_workspace_audit_log(workspace_id=ws)
    actions = {r["action"] for r in rows}
    assert actions == {"tenancy.workspace.created"}
    assert "identity.user.registered" not in actions
    assert "identity.user.password_changed" not in actions


# --- ordering (R-6 / §4.14: occurred_at descending) --------------------------
def test_rows_ordered_newest_first(make_user: UserFactory) -> None:
    user = make_user()
    ws = uuid4()
    for i in range(3):
        record_audit(
            action="tenancy.membership.added",
            actor=user,
            workspace_id=ws,
            metadata={"seq": i},
        )
    rows = read_workspace_audit_log(workspace_id=ws)
    occurred = [r["occurred_at"] for r in rows]
    assert occurred == sorted(occurred, reverse=True)


# --- filters (§4.14) ----------------------------------------------------------
def test_action_exact_filter(make_user: UserFactory) -> None:
    user, ws = make_user(), uuid4()
    record_audit(action="tenancy.api_key.created", actor=user, workspace_id=ws)
    record_audit(action="tenancy.api_key.revoked", actor=user, workspace_id=ws)
    rows = read_workspace_audit_log(workspace_id=ws, action="tenancy.api_key.revoked")
    assert [r["action"] for r in rows] == ["tenancy.api_key.revoked"]


def test_action_prefix_filter(make_user: UserFactory) -> None:
    user, ws = make_user(), uuid4()
    record_audit(action="tenancy.api_key.created", actor=user, workspace_id=ws)
    record_audit(action="tenancy.workspace.updated", actor=user, workspace_id=ws)
    rows = read_workspace_audit_log(workspace_id=ws, action_prefix="tenancy.api_key.")
    assert {r["action"] for r in rows} == {"tenancy.api_key.created"}


def test_actor_id_filter_matches_user_and_api_key(make_user: UserFactory) -> None:
    user, ws = make_user(), uuid4()
    other = make_user("other@example.com")
    record_audit(action="tenancy.membership.added", actor=user, workspace_id=ws)
    record_audit(action="tenancy.membership.removed", actor=other, workspace_id=ws)
    rows = read_workspace_audit_log(workspace_id=ws, actor_id=str(user.id))
    assert {r["action"] for r in rows} == {"tenancy.membership.added"}


def test_malformed_actor_id_returns_empty(make_user: UserFactory) -> None:
    user, ws = make_user(), uuid4()
    record_audit(action="tenancy.membership.added", actor=user, workspace_id=ws)
    assert read_workspace_audit_log(workspace_id=ws, actor_id="not-a-uuid") == []


# --- shape: matches the tenancy AuditEntrySerializer (§4.14) ------------------
def test_serialized_shape_has_actor_email_and_target_label(make_user: UserFactory) -> None:
    user = make_user("rosa@example.net")
    ws = uuid4()
    record_audit(
        action="tenancy.api_key.revoked",
        actor=user,
        workspace_id=ws,
        target={"type": "api_key", "id": str(uuid4()), "label": "lab-key (3f8a…UxKz)"},
        metadata={"revoked_by_role": "admin"},
    )
    (row,) = read_workspace_audit_log(workspace_id=ws)
    # Required serializer keys (tenancy.api.serializers.AuditEntrySerializer).
    assert set(row) >= {
        "audit_id",
        "occurred_at",
        "actor",
        "workspace_id",
        "action",
        "target",
        "metadata",
        "request_id",
    }
    assert row["actor"] == {"type": "user", "id": str(user.id), "email": "rosa@example.net"}
    assert row["target"]["label"] == "lab-key (3f8a…UxKz)"
    # The internal target_label echo is not leaked into client metadata.
    assert "target_label" not in row["metadata"]
    assert row["metadata"] == {"revoked_by_role": "admin"}


def test_request_id_presented_with_prefix(make_user: UserFactory) -> None:
    import structlog

    user, ws = make_user(), uuid4()
    rid = str(uuid4())
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        record_audit(action="tenancy.workspace.created", actor=user, workspace_id=ws)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    (row,) = read_workspace_audit_log(workspace_id=ws)
    assert row["request_id"] == f"req_{rid}"
