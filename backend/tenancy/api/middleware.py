"""Layer 1a: the workspace-context middleware (security §4.1; backend-arch §6).

One middleware, ordered after request-id and CORS, owns the **fail-closed
contextvar lifecycle**: it guarantees the active-workspace contextvar and the
Postgres GUCs are **cleared in a ``finally``** at the end of every request, so a
pooled worker thread never leaks one request's workspace into the next
(security §4.1 / database-schema §9.3 transaction-local discipline).

Arming the context (resolving the workspace from the route + membership for JWT,
or from the key for API-key auth) happens *inside the view* via
``arm_request_workspace`` — that is where DRF authentication has already run and
the authenticated principal + resolved workspace exist (DRF auth runs in the
view, after this middleware). The middleware's contract is the lifecycle: start
clean, end clean, regardless of how a view armed the context mid-request.

This is the chokepoint security §4.1 names: every request passes through it, and
no code path can leave a workspace context armed across the request boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils.functional import SimpleLazyObject

from tenancy.domain import context
from tenancy.infra import guc


def _lazy_membership_summaries(request: HttpRequest) -> Any:
    """Resolve the caller's membership summaries lazily (after DRF auth).

    Identity's ``GET /users/me`` reads ``request.membership_summaries`` to render
    the ``memberships`` array (the identity↔tenancy seam). Computed lazily so the
    authenticated ``request.user`` exists by access time; returns ``[]`` for
    unauthenticated requests.
    """
    from tenancy.application import services

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    return services.membership_summaries(user)


class WorkspaceContextMiddleware:
    """Fail-closed workspace-context lifecycle (security §4.1).

    Also installs the identity↔tenancy seams Identity's account views read:
    ``request.sole_admin_guard`` (INV-ID-4/INV-TEN-3) and
    ``request.membership_summaries`` (``GET /users/me``).
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Ensure a clean slate at entry (defensive: a prior request that crashed
        # before its finally should never poison this one).
        context.deactivate(context.activate(None))  # type: ignore[arg-type]
        from tenancy.application import services

        request.sole_admin_guard = services.sole_admin_guard  # type: ignore[attr-defined]
        request.membership_summaries = SimpleLazyObject(  # type: ignore[attr-defined]
            lambda: _lazy_membership_summaries(request)
        )
        # Snapshot the GUCs as they stand at request entry so the finally can
        # RESTORE them rather than blindly clearing to empty. In production each
        # request is its own ATOMIC_REQUESTS transaction with no GUC armed yet, so
        # the snapshot is empty and restore == clear — the fail-closed guarantee is
        # preserved (no request leaks its workspace into the next). When the request
        # runs *inside* an already-armed transaction (the integration test lane wraps
        # each test in one transaction and arms a workspace in its fixture), restore
        # leaves that enclosing context intact instead of disarming it — ``SET LOCAL``
        # is transaction-scoped, so an unconditional clear would wipe the GUC for the
        # whole enclosing transaction and blind every post-request armed read. This is
        # the Layer-2 GUC twin of the contextvar's reset(token) discipline below.
        prior_workspace = guc.get_workspace_guc()
        prior_user = guc.get_user_guc()
        prior_platform = guc.get_platform_guc()
        prior_api_key_prefix = guc.get_api_key_prefix_guc()
        try:
            return self.get_response(request)
        finally:
            # Always restore the entry snapshot — the fail-closed guarantee. The
            # contextvar is reset to None (its own snapshot is taken at the top via
            # activate(None)); the transaction-local GUCs are returned to their
            # pre-request values (empty in production → cleared).
            context._active_workspace_id.set(None)
            guc.restore_workspace_guc(prior_workspace)
            guc.restore_user_guc(prior_user)
            guc.restore_platform_guc(prior_platform)
            guc.restore_api_key_prefix_guc(prior_api_key_prefix)


def arm_request_workspace(request: HttpRequest, workspace_id: uuid.UUID) -> None:
    """Arm Layer 1 (contextvar) + Layer 2 (GUCs) for ``workspace_id``.

    Called by viewsets after they have authenticated the principal and resolved
    + authorized the target workspace. Sets ``request.workspace_id`` (read by
    Layer 3 object perms), the active contextvar (Layer 1b scoped managers), and
    the Postgres GUCs (Layer 2 RLS) for the request transaction.
    """
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None) if getattr(user, "is_authenticated", False) else None
    request.workspace_id = workspace_id  # type: ignore[attr-defined]
    context.activate(workspace_id)
    guc.set_request_gucs(
        user_id=user_id if isinstance(user_id, uuid.UUID) else None,
        workspace_id=workspace_id,
    )
