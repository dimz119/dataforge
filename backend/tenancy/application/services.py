"""Use-case services for the Tenancy context (workspaces + memberships).

Services own the transaction boundary (``ATOMIC_REQUESTS`` already wraps the
request; these add nested ``atomic`` blocks where a sub-step must be all-or-
nothing). Audit entries are written in the SAME transaction as the mutation
(INV-AUD-2). The sole-admin rule (INV-TEN-3) is enforced under
``SELECT … FOR UPDATE`` on the workspace's admin memberships.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from config.problems import ConflictError, NotFoundError, PermissionDeniedError
from identity.application.permissions import require_verified
from identity.domain.models import User
from tenancy.application.audit import emit
from tenancy.domain.context import workspace_context
from tenancy.domain.ids import uuid4
from tenancy.domain.models import (
    FREE_QUOTA_DEFAULTS,
    ROLE_ADMIN,
    ROLE_MEMBER,
    Membership,
    Workspace,
    WorkspaceQuotas,
)
from tenancy.infra import guc

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")


@contextmanager
def worker_workspace_scope(workspace_id: uuid.UUID | None) -> Iterator[None]:
    """Arm a workspace for a Celery task body (Layer-1 contextvar + Layer-2 GUC).

    A web request arms RLS in two layers inside its ``ATOMIC_REQUESTS`` transaction:
    the scoped-manager contextvar (Layer 1) AND the Postgres GUC ``app.workspace_id``
    that the RLS policies read (Layer 2). A Celery task has neither — so a task that
    only set the contextvar would pass the scoped-manager filter but be hidden by
    RLS (``app_workspace_id()`` is NULL → the row is invisible to the NOBYPASSRLS
    runtime role), reading rows as "not found".

    This opens one ``transaction.atomic()`` (the GUC is ``SET LOCAL``, so it must
    live inside a transaction on the same connection the ORM uses), sets the GUC,
    and arms the contextvar — both cleared on exit. ``workspace_id=None`` (a global
    builtin) arms neither (the row carries a NULL workspace; RLS admits it via the
    ``workspace_id IS NULL`` branch).
    """
    if workspace_id is None:
        with transaction.atomic():
            yield
        return
    with transaction.atomic(), workspace_context(workspace_id):
        # Save the prior GUC and RESTORE it on exit (not blindly clear to ''):
        # ``SET LOCAL`` is transaction-scoped, not savepoint-scoped, so a nested
        # scope that cleared on exit would wipe the GUC for the *enclosing*
        # transaction too — hiding every subsequent armed read/write under the
        # NOBYPASSRLS runtime role. Restoring the previous value makes this scope
        # nesting-safe, the Layer-2 GUC twin of Layer-1's contextvar reset(token)
        # (tenancy.domain.context.workspace_context — "exit restores the previous
        # value, not unconditionally None"). The runner arms an outer scope per
        # tick and the per-write seams (recorder/buffer/ledger/checkpoint) re-arm
        # the same workspace inside it; without restore the second seam blinds the
        # third.
        prior = guc.get_workspace_guc()
        guc.set_workspace_guc(workspace_id)
        try:
            yield
        finally:
            guc.restore_workspace_guc(prior)


@contextmanager
def platform_read_scope() -> Iterator[None]:
    """Arm the platform-read GUC for a cross-tenant pre-context SELECT (§4.2 / §8.3).

    Two trusted platform paths read rows BEFORE any workspace can be armed:

    * the runner data plane — its per-tick claimable scan + per-shard desired/
      checkpoint reads span every workspace's shards (INV-STR-6); and
    * the flat single-resource API routes — they must resolve a resource's owning
      workspace from its unique id before they can arm the scoped context.

    Both are hidden by the strict Class T policy under the NOBYPASSRLS runtime role.
    This opens one ``transaction.atomic()`` (the GUC is ``SET LOCAL``, so it must
    live inside a transaction on the ORM connection), sets ``app.platform = 'on'``,
    and clears it on exit. It widens READ visibility only — WITH CHECK stays
    workspace-scoped, so a write inside this scope still requires a real armed
    workspace and can never cross tenants.
    """
    with transaction.atomic():
        # Save + restore the prior value (not blindly clear) so a platform read
        # nested inside an enclosing scope leaves that scope's context intact on
        # exit — ``SET LOCAL`` is transaction-scoped, so clearing would otherwise
        # disarm the enclosing transaction (mirrors worker_workspace_scope).
        prior = guc.get_platform_guc()
        guc.set_platform_guc(True)
        try:
            yield
        finally:
            guc.restore_platform_guc(prior)


def _arm_user_for_membership_read(user: User) -> None:
    """Arm ``app.user_id`` so a caller can read their OWN membership rows.

    Membership resolution is the RLS bootstrap step: before any workspace is
    known, the caller must read their own memberships to discover/authorize one.
    The Class M policy admits this via its ``user_id = app_user_id()`` branch
    (tenancy.infra.rls / database-schema §9.5), but only once ``app.user_id`` is
    set. The workspace-context middleware arms the GUCs *after* workspace
    resolution, so these self-membership reads — run under the NOBYPASSRLS runtime
    role (SEC-TEN-2) — would otherwise hit RLS default-deny and resolve to "no
    membership" → a spurious 404. Arming the user GUC here (transaction-local,
    to the caller's own id only) is the documented two-phase arming the null-safe
    accessors (§9.3) are designed for; it unlocks nothing but the caller's own
    membership rows. No-op off Postgres.
    """
    if isinstance(user.id, uuid.UUID):
        guc.set_user_guc(user.id)


def derive_slug(name: str) -> str:
    """Derive a candidate slug from a workspace name (api-spec §4.3)."""
    base = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower())
    base = re.sub(r"-+", "-", base).strip("-")
    if not base:
        base = "workspace"
    if not base[0].isalpha():
        base = f"w-{base}"
    return base[:40].rstrip("-")


def _membership_summary(membership: Membership, workspace: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": str(workspace.id),
        "name": workspace.name,
        "slug": workspace.slug,
        "role": membership.role,
    }


def list_user_workspaces(user: User) -> list[dict[str, Any]]:
    """The caller's workspaces with their role + member count (api-spec §4.3).

    Cross-workspace by nature (a user's memberships span workspaces), so it uses
    the unscoped manager and filters by ``user`` explicitly.
    # tenancy: unscoped — a user's own memberships legitimately span workspaces.
    """
    _arm_user_for_membership_read(user)
    rows: list[dict[str, Any]] = []
    memberships = (
        Membership.all_objects.filter(user=user, workspace__deleted_at__isnull=True)
        .select_related("workspace")
        .order_by("-workspace__created_at")
    )
    for m in memberships:
        ws = m.workspace
        # Arm the workspace GUC for an accurate member count: the Class M policy
        # otherwise only admits the caller's own row (app.user_id branch), so the
        # count must run with this workspace armed (the caller is a member → safe).
        guc.set_workspace_guc(ws.id)
        member_count = Membership.all_objects.filter(workspace=ws).count()
        rows.append(
            {
                "workspace_id": str(ws.id),
                "name": ws.name,
                "slug": ws.slug,
                "plan": ws.plan,
                "role": m.role,
                "member_count": member_count,
                "created_at": ws.created_at,
            }
        )
    # Leave only the user GUC armed (reset the per-iteration workspace GUC) so a
    # stale workspace context never lingers if more work runs in this transaction.
    guc.set_workspace_guc(None)
    return rows


def membership_summaries(user: User) -> list[dict[str, Any]]:
    """The ``memberships`` array for ``GET /users/me`` (api-spec §4.2)."""
    _arm_user_for_membership_read(user)
    summaries: list[dict[str, Any]] = []
    memberships = (
        Membership.all_objects.filter(  # tenancy: unscoped — own memberships span workspaces.
            user=user, workspace__deleted_at__isnull=True
        )
        .select_related("workspace")
        .order_by("-workspace__created_at")
    )
    for m in memberships:
        summaries.append(_membership_summary(m, m.workspace))
    return summaries


def get_membership(workspace_id: uuid.UUID, user: User) -> Membership | None:
    """The caller's membership in ``workspace_id`` (unscoped lookup by both keys).

    # tenancy: unscoped — membership resolution precedes workspace-context arming.
    """
    _arm_user_for_membership_read(user)
    return (
        Membership.all_objects.filter(
            workspace_id=workspace_id, user=user, workspace__deleted_at__isnull=True
        )
        .select_related("workspace")
        .first()
    )


@transaction.atomic
def create_workspace(*, user: User, name: str, slug: str | None) -> Workspace:
    """Create a workspace, its creator-admin membership, and Free quotas (§9.4).

    INV-ID-2: verified email required (403 email-not-verified). The new id is
    generated app-side and the GUC is armed *first* so the RLS ``WITH CHECK``
    policies pass within the create txn (database-schema §9.4). Audit
    ``tenancy.workspace.created`` in the same transaction (INV-AUD-2).
    """
    require_verified(user)  # INV-ID-2 → 403 email-not-verified

    workspace_id = uuid4()
    candidate = (slug or derive_slug(name)).lower()
    if not _SLUG_RE.match(candidate):
        from rest_framework.exceptions import ErrorDetail
        from rest_framework.exceptions import ValidationError as DRFValidationError

        raise DRFValidationError(
            {"slug": [ErrorDetail("Invalid workspace slug.", code="invalid")]}
        )

    # Arm both GUCs before the inserts (database-schema §9.4) and the contextvar
    # so the scoped managers admit the creator-membership/quota writes.
    guc.set_request_gucs(user_id=user.id, workspace_id=workspace_id)
    with workspace_context(workspace_id):
        workspace = Workspace(
            id=workspace_id, name=name, slug=candidate, plan="free", created_by=user
        )
        try:
            workspace.save(force_insert=True)
        except IntegrityError as exc:
            raise ConflictError("A workspace with this slug already exists.") from exc
        Membership.objects.create(
            id=uuid4(), workspace=workspace, user=user, role=ROLE_ADMIN
        )
        WorkspaceQuotas.objects.create(workspace=workspace, **FREE_QUOTA_DEFAULTS)
        emit(
            "tenancy.workspace.created",
            actor=user,
            workspace_id=workspace_id,
            target={"type": "workspace", "id": str(workspace_id), "label": candidate},
            metadata={"slug": candidate, "plan": "free"},
        )
    return workspace


@transaction.atomic
def rename_workspace(*, workspace: Workspace, name: str, actor: User) -> Workspace:
    """Rename a workspace (slug immutable; admin-only enforced by the view)."""
    workspace.name = name
    workspace.save(update_fields=["name", "updated_at"])
    emit(
        "tenancy.workspace.updated",
        actor=actor,
        workspace_id=workspace.id,
        target={"type": "workspace", "id": str(workspace.id), "label": workspace.slug},
        metadata={"name": name},
    )
    return workspace


@transaction.atomic
def delete_workspace(*, workspace: Workspace, actor: User) -> None:
    """Soft-delete (tombstone) a workspace and cascade key revocation (INV-TEN-6).

    Admin-only (enforced by the view). Revokes all keys, tombstones the workspace;
    stream stop / buffer drop land with those contexts in later phases (commented
    deferred). Audit ``tenancy.workspace.deleted`` is the tombstone (never dropped).
    """
    from tenancy.application import keys as key_service

    key_service.revoke_all_workspace_keys(workspace=workspace, actor=actor)
    # Stream stop + buffer/checkpoint drop on workspace deletion land with the
    # Stream Control / Delivery contexts — Phase 5/6 (INV-TEN-6 cascade).
    workspace.deleted_at = timezone.now()
    workspace.save(update_fields=["deleted_at", "updated_at"])
    emit(
        "tenancy.workspace.deleted",
        actor=actor,
        workspace_id=workspace.id,
        target={"type": "workspace", "id": str(workspace.id), "label": workspace.slug},
        metadata={},
    )


# --- memberships -------------------------------------------------------------
def _admin_count_for_update(workspace: Workspace) -> int:
    """Count admin memberships under ``SELECT … FOR UPDATE`` (INV-TEN-3)."""
    # tenancy: unscoped — the sole-admin guard locks the workspace's admin rows.
    return (
        Membership.all_objects.select_for_update()
        .filter(workspace=workspace, role=ROLE_ADMIN)
        .count()
    )


def list_members(workspace: Workspace) -> list[dict[str, Any]]:
    """Members of ``workspace`` (any member may list; api-spec §4.3)."""
    rows: list[dict[str, Any]] = []
    memberships = (
        Membership.all_objects.filter(workspace=workspace)  # tenancy: unscoped — view armed context
        .select_related("user")
        .order_by("created_at")
    )
    for m in memberships:
        rows.append(
            {
                "user_id": str(m.user_id),
                "email": m.user.email,
                "role": m.role,
                "joined_at": m.created_at,
            }
        )
    return rows


@transaction.atomic
def add_member(*, workspace: Workspace, email: str, role: str, actor: User) -> Membership:
    """Add an existing verified user to ``workspace`` (api-spec §4.3, Phase 2).

    The email must belong to an existing verified account → 404 with the
    documented detail otherwise. Duplicate membership → 409 (INV-TEN-2).
    """
    from identity.domain.email import normalize_email

    normalized = normalize_email(email)
    invitee = User.objects.filter(
        email=normalized, deleted_at__isnull=True, is_verified=True
    ).first()
    if invitee is None:
        raise NotFoundError(
            "no verified account for this email — the user must sign up first"
        )
    if role not in (ROLE_ADMIN, ROLE_MEMBER):
        role = ROLE_MEMBER
    try:
        membership: Membership = Membership.objects.create(
            id=uuid4(), workspace=workspace, user=invitee, role=role
        )
    except IntegrityError as exc:
        raise ConflictError("This user is already a member of the workspace.") from exc
    emit(
        "tenancy.membership.added",
        actor=actor,
        workspace_id=workspace.id,
        target={"type": "user", "id": str(invitee.id), "label": invitee.email},
        metadata={"role": role},
    )
    return membership


@transaction.atomic
def change_member_role(
    *, workspace: Workspace, target_user_id: uuid.UUID, role: str, actor: User
) -> Membership:
    """Change a member's role; last-admin demotion → 409 (INV-TEN-3)."""
    membership: Membership | None = (
        Membership.all_objects.select_for_update()  # tenancy: unscoped — locks the membership row
        .filter(workspace=workspace, user_id=target_user_id)
        .first()
    )
    if membership is None:
        raise NotFoundError()
    if role not in (ROLE_ADMIN, ROLE_MEMBER):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        raise DRFValidationError({"role": ["Must be 'admin' or 'member'."]})
    if membership.role == ROLE_ADMIN and role == ROLE_MEMBER:
        # Demoting an admin: refuse if it would leave zero admins (INV-TEN-3).
        if _admin_count_for_update(workspace) <= 1:
            raise ConflictError("Cannot demote the last admin of the workspace.")
    membership.role = role
    membership.save(update_fields=["role"])
    emit(
        "tenancy.membership.role_changed",
        actor=actor,
        workspace_id=workspace.id,
        target={"type": "user", "id": str(target_user_id), "label": membership.user.email},
        metadata={"role": role},
    )
    return membership


@transaction.atomic
def remove_member(
    *, workspace: Workspace, target_user_id: uuid.UUID, actor: User
) -> None:
    """Remove a member (admin removes anyone; member self-leaves). INV-TEN-3."""
    membership = (
        Membership.all_objects.select_for_update()  # tenancy: unscoped — locks the membership row
        .filter(workspace=workspace, user_id=target_user_id)
        .select_related("user")
        .first()
    )
    if membership is None:
        raise NotFoundError()
    if membership.role == ROLE_ADMIN and _admin_count_for_update(workspace) <= 1:
        raise ConflictError("Cannot remove the last admin of the workspace.")
    email = membership.user.email
    membership.delete()
    emit(
        "tenancy.membership.removed",
        actor=actor,
        workspace_id=workspace.id,
        target={"type": "user", "id": str(target_user_id), "label": email},
        metadata={},
    )


def sole_admin_blocking_workspaces(user: User) -> list[dict[str, str]]:
    """Workspaces where ``user`` is the sole admin (blocks account deletion).

    The hook the Identity ``DELETE /users/me`` view invokes via
    ``request.sole_admin_guard`` (INV-ID-4 / INV-TEN-3).
    # tenancy: unscoped — scans every workspace the user admins, cross-workspace.
    """
    blocking: list[dict[str, str]] = []
    admin_memberships = (
        Membership.all_objects.filter(
            user=user, role=ROLE_ADMIN, workspace__deleted_at__isnull=True
        )
        .select_related("workspace")
    )
    for m in admin_memberships:
        ws = m.workspace
        admin_total = Membership.all_objects.filter(workspace=ws, role=ROLE_ADMIN).count()
        if admin_total <= 1:
            blocking.append({"workspace_id": str(ws.id), "name": ws.name, "slug": ws.slug})
    return blocking


def sole_admin_guard(user: User) -> None:
    """Raise 409 conflict naming the blocking workspaces (INV-ID-4 / INV-TEN-3)."""
    blocking = sole_admin_blocking_workspaces(user)
    if blocking:
        names = ", ".join(b["slug"] for b in blocking)
        raise ConflictError(
            f"You are the sole admin of: {names}. Transfer or delete them first."
        )


__all__ = [
    "PermissionDeniedError",
    "add_member",
    "change_member_role",
    "create_workspace",
    "delete_workspace",
    "derive_slug",
    "get_membership",
    "list_members",
    "list_user_workspaces",
    "membership_summaries",
    "remove_member",
    "rename_workspace",
    "sole_admin_blocking_workspaces",
    "sole_admin_guard",
]
