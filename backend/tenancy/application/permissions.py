"""Layer 3: DRF object-level permission classes (security §4.3).

The authorization layer — distinct from isolation (Layers 1+2). These classes
implement the §3.3 policy table:

* foreign-workspace object → 404 (the scoped queryset returns no row; the
  ``has_object_permission`` re-assert below is the belt over that suspenders);
* insufficient scope (key) → 403 ``permission-denied`` + ``required_scope``;
* insufficient role (JWT)  → 403 ``permission-denied`` + ``required_role``.

``HasKeyScope`` reads the endpoint's declared ``required_scopes`` (set on the
view) and the key's resolved scopes (``request.api_key_scopes``); JWT principals
satisfy it vacuously (console convenience reads, A-5). Role checks
(``IsWorkspaceAdmin``) apply only to JWT principals — an API key cannot manage
workspaces/members/keys at all (those surfaces never parse the key header).
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from config.problems import PermissionDeniedError
from tenancy.domain.models import ROLE_ADMIN


def _is_key_principal(request: Request) -> bool:
    # Duck-typed: an API-key principal carries ``api_key_id`` + ``scopes`` and is
    # not a Django user. Avoids importing the api layer from application/
    # (preserves the app-layering import contract).
    user = getattr(request, "user", None)
    return user is not None and hasattr(user, "api_key_id") and hasattr(user, "scopes")


class IsAuthenticatedPrincipal(BasePermission):
    """A valid JWT user or API key resolved (security §4.3)."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = getattr(request, "user", None)
        return bool(user is not None and getattr(user, "is_authenticated", False))


class HasKeyScope(BasePermission):
    """The key's scope set covers the view's ``required_scopes`` (security §3.2.2).

    JWT principals pass vacuously (A-5). A key lacking a required scope within its
    own workspace → 403 ``permission-denied`` naming the missing ``required_scope``.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not _is_key_principal(request):
            return True  # JWT (or no key) — scope gating does not apply
        required = set(getattr(view, "required_scopes", ()) or ())
        if not required:
            return True
        held = set(getattr(request, "api_key_scopes", ()) or ())
        missing = required - held
        if missing:
            raise PermissionDeniedError(
                "The API key lacks a required scope.",
                required_scope=sorted(missing)[0],
            )
        return True


class IsWorkspaceAdmin(BasePermission):
    """The JWT caller is an ``admin`` of the request's workspace (security §4.3).

    Reads ``request.workspace_role`` (set by the middleware/view from the caller's
    membership). API-key principals never reach admin surfaces (those endpoints do
    not parse the key header), so a key principal here is treated as forbidden.
    Insufficient role within an accessible workspace → 403 ``required_role=admin``.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        role = getattr(request, "workspace_role", None)
        if role == ROLE_ADMIN:
            return True
        raise PermissionDeniedError(
            "This action requires the workspace admin role.", required_role=ROLE_ADMIN
        )


class HasObjectWorkspace(BasePermission):
    """Re-assert ``obj.workspace_id ∈ caller's scope`` on every object (§4.3).

    The belt over the already-scoped queryset: a future ``all_objects`` misuse in
    a viewset still cannot serve a foreign object. Mismatch → treated as absent
    (the caller maps to 404 via the scoped lookup, never 403).
    """

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        allowed = getattr(request, "workspace_id", None)
        obj_ws = getattr(obj, "workspace_id", None) or getattr(obj, "id", None)
        return allowed is not None and obj_ws == allowed
