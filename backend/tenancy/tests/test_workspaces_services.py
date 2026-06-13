"""Workspace + membership service invariants (INV-TEN-2/3, INV-ID-2, quotas)."""

from __future__ import annotations

import pytest

from config.problems import ConflictError, EmailNotVerified, NotFoundError
from tenancy.application import services
from tenancy.domain.context import workspace_context
from tenancy.domain.models import (
    FREE_QUOTA_DEFAULTS,
    ROLE_ADMIN,
    ROLE_MEMBER,
    Membership,
    WorkspaceQuotas,
)

pytestmark = pytest.mark.django_db


def test_create_workspace_seeds_free_quota_and_admin_membership(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user("creator@example.com", is_verified=True)
    ws = services.create_workspace(user=user, name="Ada Lab", slug=None)

    quota = WorkspaceQuotas.all_objects.get(workspace=ws)
    assert quota.max_concurrent_streams == FREE_QUOTA_DEFAULTS["max_concurrent_streams"]
    assert quota.max_api_keys == FREE_QUOTA_DEFAULTS["max_api_keys"]
    assert ws.plan == "free"

    membership = Membership.all_objects.get(workspace=ws, user=user)
    assert membership.role == ROLE_ADMIN


def test_unverified_user_cannot_create_workspace(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user("unverified@example.com", is_verified=False)
    with pytest.raises(EmailNotVerified):
        services.create_workspace(user=user, name="Nope", slug=None)


def test_duplicate_slug_conflicts(make_user) -> None:  # type: ignore[no-untyped-def]
    u1 = make_user("u1@example.com", is_verified=True)
    u2 = make_user("u2@example.com", is_verified=True)
    services.create_workspace(user=u1, name="Shared", slug="shared-lab")
    with pytest.raises(ConflictError):
        services.create_workspace(user=u2, name="Shared2", slug="shared-lab")


def test_add_member_requires_existing_verified_account(make_workspace, make_user) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("owner@example.com")
    with workspace_context(setup.workspace.id):
        # Unknown email → 404 with the documented detail (api-spec §4.3).
        with pytest.raises(NotFoundError):
            services.add_member(
                workspace=setup.workspace,
                email="ghost@example.com",
                role="member",
                actor=setup.admin,
            )
        invitee = make_user("invitee@example.com", is_verified=True)
        membership = services.add_member(
            workspace=setup.workspace, email=invitee.email, role="member", actor=setup.admin
        )
        assert membership.role == ROLE_MEMBER


def test_duplicate_membership_conflicts(make_workspace, make_user) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("owner2@example.com")
    invitee = make_user("dup@example.com", is_verified=True)
    with workspace_context(setup.workspace.id):
        services.add_member(
            workspace=setup.workspace, email=invitee.email, role="member", actor=setup.admin
        )
        with pytest.raises(ConflictError):  # INV-TEN-2
            services.add_member(
                workspace=setup.workspace, email=invitee.email, role="member", actor=setup.admin
            )


def test_cannot_demote_last_admin(make_workspace) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("solo@example.com")
    with workspace_context(setup.workspace.id):
        with pytest.raises(ConflictError):  # INV-TEN-3
            services.change_member_role(
                workspace=setup.workspace,
                target_user_id=setup.admin.id,
                role=ROLE_MEMBER,
                actor=setup.admin,
            )


def test_cannot_remove_last_admin(make_workspace) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("solo2@example.com")
    with workspace_context(setup.workspace.id):
        with pytest.raises(ConflictError):  # INV-TEN-3
            services.remove_member(
                workspace=setup.workspace, target_user_id=setup.admin.id, actor=setup.admin
            )


def test_second_admin_can_be_demoted(make_workspace, make_user) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("multi@example.com")
    second = make_user("second-admin@example.com", is_verified=True)
    with workspace_context(setup.workspace.id):
        services.add_member(
            workspace=setup.workspace, email=second.email, role=ROLE_ADMIN, actor=setup.admin
        )
        membership = services.change_member_role(
            workspace=setup.workspace,
            target_user_id=second.id,
            role=ROLE_MEMBER,
            actor=setup.admin,
        )
        assert membership.role == ROLE_MEMBER


def test_sole_admin_guard_blocks_deletion(make_workspace) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("blockme@example.com")
    blocking = services.sole_admin_blocking_workspaces(setup.admin)
    assert any(b["workspace_id"] == str(setup.workspace.id) for b in blocking)
    with pytest.raises(ConflictError):
        services.sole_admin_guard(setup.admin)


def test_sole_admin_guard_passes_after_transfer(make_workspace, make_user) -> None:  # type: ignore[no-untyped-def]
    setup = make_workspace("transfer@example.com")
    other = make_user("co-admin@example.com", is_verified=True)
    with workspace_context(setup.workspace.id):
        services.add_member(
            workspace=setup.workspace, email=other.email, role=ROLE_ADMIN, actor=setup.admin
        )
    # Now there are two admins → original is no longer the sole admin.
    services.sole_admin_guard(setup.admin)  # does not raise
