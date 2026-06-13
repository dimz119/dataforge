"""Tenancy API views (api-spec §4.3 workspaces/members, §4.4 quotas,
§4.5 api-keys, §4.14 audit-log, phase-doc key-info).

Workspace/member/key/quota/audit management is the **JWT-only console surface**
(ADR-0011): these views set ``authentication_classes = [DataForgeJWTAuthentication]``
so the API-key header is never parsed here — a key on these surfaces is an absent
credential → 401 (SEC-AUTH-1). ``GET /auth/key-info`` is the inverse: the
data-plane probe, API-key-only.

The 401/403/404 policy (security §3.3) is implemented uniformly:
foreign-workspace access → 404 (the membership lookup returns nothing);
insufficient role within an accessible workspace → 403 with ``required_role``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import NotFoundError, PermissionDeniedError
from identity.application.permissions import IsVerified
from identity.domain.models import User
from identity.infra.jwt import DataForgeJWTAuthentication
from tenancy.api import serializers
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace
from tenancy.application import keys as key_service
from tenancy.application import services
from tenancy.domain.models import ROLE_ADMIN, Workspace


def _user(request: Request) -> User:
    return cast(User, request.user)


def _validated(serializer_cls: type, request: Request) -> dict[str, Any]:
    serializer = serializer_cls(data=request.data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _resolve_workspace(request: Request, workspace_id: str) -> tuple[Workspace, str]:
    """Resolve + authorize the caller's access to ``workspace_id``.

    Returns ``(workspace, caller_role)``. Foreign/absent workspace, or one the
    caller is not a member of, → 404 (W-3 masking — existence never confirmed).
    Arms the workspace context (Layers 1+2) on success and stashes the role for
    the Layer 3 admin checks.
    """
    try:
        ws_uuid = uuid.UUID(str(workspace_id))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc
    membership = services.get_membership(ws_uuid, _user(request))
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()  # foreign / absent / deleted → 404
    workspace = membership.workspace
    request.workspace_role = membership.role  # type: ignore[attr-defined]
    arm_request_workspace(request._request, workspace.id)
    return workspace, membership.role


def _require_admin(role: str) -> None:
    if role != ROLE_ADMIN:
        raise PermissionDeniedError(
            "This action requires the workspace admin role.", required_role=ROLE_ADMIN
        )


def _serialize_workspace(
    workspace: Workspace, role: str, *, member_count: int | None = None
) -> Any:
    from tenancy.domain.models import Membership

    if member_count is None:
        member_count = Membership.all_objects.filter(  # tenancy: unscoped — count of armed ws
            workspace=workspace
        ).count()
    return serializers.WorkspaceSerializer(
        {
            "workspace_id": workspace.id,
            "name": workspace.name,
            "slug": workspace.slug,
            "plan": workspace.plan,
            "role": role,
            "member_count": member_count,
            "created_at": workspace.created_at,
        }
    ).data


class WorkspaceCollectionView(APIView):
    """GET/POST /workspaces (api-spec §4.3, #12-13)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="workspaces_list", responses={200: serializers.WorkspaceSerializer(many=True)}
    )
    def get(self, request: Request) -> Response:
        rows = services.list_user_workspaces(_user(request))
        data = serializers.WorkspaceSerializer(rows, many=True).data  # type: ignore[arg-type]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="workspaces_create",
        request=serializers.WorkspaceCreateSerializer,
        responses={201: serializers.WorkspaceSerializer},
    )
    def post(self, request: Request) -> Response:
        # Verified email required (INV-ID-2); the service re-checks (defence-in-depth).
        IsVerified().has_permission(request, self)
        data = _validated(serializers.WorkspaceCreateSerializer, request)
        workspace = services.create_workspace(
            user=_user(request), name=data["name"], slug=data.get("slug")
        )
        body = _serialize_workspace(workspace, ROLE_ADMIN, member_count=1)
        response = Response(body, status=status.HTTP_201_CREATED)
        response["Location"] = f"/api/v1/workspaces/{workspace.id}"
        return response


class WorkspaceDetailView(APIView):
    """GET/PATCH/DELETE /workspaces/{id} (api-spec §4.3, #14-16)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="workspaces_retrieve", responses={200: serializers.WorkspaceSerializer}
    )
    def get(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        return Response(_serialize_workspace(workspace, role))

    @extend_schema(
        operation_id="workspaces_update",
        request=serializers.WorkspaceRenameSerializer,
        responses={200: serializers.WorkspaceSerializer},
    )
    def patch(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        _require_admin(role)
        data = _validated(serializers.WorkspaceRenameSerializer, request)
        workspace = services.rename_workspace(
            workspace=workspace, name=data["name"], actor=_user(request)
        )
        return Response(_serialize_workspace(workspace, role))

    @extend_schema(operation_id="workspaces_delete", responses={204: None})
    def delete(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        _require_admin(role)
        # X-Confirm-Delete guard (api-spec §4.3): must equal the workspace id.
        confirm = request.headers.get("X-Confirm-Delete")
        if confirm != str(workspace.id):
            from rest_framework.exceptions import ValidationError as DRFValidationError

            raise DRFValidationError(
                {"X-Confirm-Delete": ["Header must equal the workspace id to confirm deletion."]}
            )
        services.delete_workspace(workspace=workspace, actor=_user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


def _serialize_membership(membership: Any) -> Any:
    return serializers.MembershipSerializer(
        {
            "user_id": membership.user_id,
            "email": membership.user.email,
            "role": membership.role,
            "joined_at": membership.created_at,
        }
    ).data


class MembershipCollectionView(APIView):
    """GET/POST /workspaces/{id}/members (api-spec §4.3, #17-18)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="members_list", responses={200: serializers.MembershipSerializer(many=True)}
    )
    def get(self, request: Request, workspace_id: str) -> Response:
        workspace, _role = _resolve_workspace(request, workspace_id)
        rows = services.list_members(workspace)
        data = serializers.MembershipSerializer(rows, many=True).data  # type: ignore[arg-type]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="members_add",
        request=serializers.MemberAddSerializer,
        responses={201: serializers.MembershipSerializer},
    )
    def post(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        _require_admin(role)
        data = _validated(serializers.MemberAddSerializer, request)
        membership = services.add_member(
            workspace=workspace, email=data["email"], role=data["role"], actor=_user(request)
        )
        return Response(_serialize_membership(membership), status=status.HTTP_201_CREATED)


class MembershipDetailView(APIView):
    """PATCH/DELETE /workspaces/{id}/members/{user_id} (api-spec §4.3, #19-20)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="members_update",
        request=serializers.MemberRoleSerializer,
        responses={200: serializers.MembershipSerializer},
    )
    def patch(self, request: Request, workspace_id: str, user_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        _require_admin(role)
        data = _validated(serializers.MemberRoleSerializer, request)
        membership = services.change_member_role(
            workspace=workspace,
            target_user_id=uuid.UUID(user_id),
            role=data["role"],
            actor=_user(request),
        )
        return Response(_serialize_membership(membership))

    @extend_schema(operation_id="members_remove", responses={204: None})
    def delete(self, request: Request, workspace_id: str, user_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        target = uuid.UUID(user_id)
        # Admin removes anyone; a member may remove only themself (self-leave).
        if role != ROLE_ADMIN and target != _user(request).id:
            raise PermissionDeniedError(
                "Only an admin may remove other members.", required_role=ROLE_ADMIN
            )
        services.remove_member(workspace=workspace, target_user_id=target, actor=_user(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApiKeyCollectionView(APIView):
    """POST/GET /workspaces/{id}/api-keys (api-spec §4.5, #22-23)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="api_keys_list",
        responses={200: serializers.ApiKeyListItemSerializer(many=True)},
    )
    def get(self, request: Request, workspace_id: str) -> Response:
        workspace, _role = _resolve_workspace(request, workspace_id)
        rows = key_service.list_keys(workspace=workspace)
        data = [
            serializers.ApiKeyListItemSerializer(
                {
                    "api_key_id": k.id,
                    "name": k.name,
                    "prefix": k.key_prefix.split("_")[-1],
                    "last4": k.last4,
                    "scopes": list(k.scopes),
                    "state": k.state,
                    "last_used_at": k.last_used_at,
                    "expires_at": k.expires_at,
                    "created_by": k.created_by_id,
                    "created_at": k.created_at,
                }
            ).data
            for k in rows
        ]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="api_keys_create",
        request=serializers.ApiKeyCreateSerializer,
        responses={201: serializers.ApiKeyCreatedSerializer},
    )
    def post(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        # Any verified member may create a key (api-spec §4.5); IsVerified gate.
        IsVerified().has_permission(request, self)
        data = _validated(serializers.ApiKeyCreateSerializer, request)
        api_key, plaintext = key_service.create_key(
            workspace=workspace,
            actor=_user(request),
            name=data["name"],
            scopes=data["scopes"],
            expires_at=data.get("expires_at"),
            actor_role=role,
        )
        body = serializers.ApiKeyCreatedSerializer(
            {
                "api_key_id": api_key.id,
                "workspace_id": workspace.id,
                "name": api_key.name,
                "key": plaintext,  # reveal-once (SEC-KEY-4)
                "prefix": api_key.key_prefix.split("_")[-1],
                "last4": api_key.last4,
                "scopes": list(api_key.scopes),
                "state": api_key.state,
                "expires_at": api_key.expires_at,
                "created_by": api_key.created_by_id,
                "created_at": api_key.created_at,
            }
        ).data
        response = Response(body, status=status.HTTP_201_CREATED)
        response["Location"] = f"/api/v1/workspaces/{workspace.id}/api-keys/{api_key.id}"
        return response


class ApiKeyDetailView(APIView):
    """DELETE /workspaces/{id}/api-keys/{api_key_id} — revoke (api-spec §4.5, #24)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id="api_keys_revoke", responses={204: None})
    def delete(self, request: Request, workspace_id: str, api_key_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        key_service.revoke_key(
            workspace=workspace,
            api_key_id=uuid.UUID(api_key_id),
            actor=_user(request),
            actor_role=role,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class QuotaView(APIView):
    """GET /workspaces/{id}/quotas (api-spec §4.4). JWT | Key(streams:read).

    ApiKey auth listed first so both-headers → 400 ambiguous-credentials (A-2).
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: serializers.QuotaSerializer})
    def get(self, request: Request, workspace_id: str) -> Response:
        from tenancy.domain.models import WorkspaceQuotas

        ws = _resolve_for_read(request, workspace_id, required_scope="streams:read")
        quota = WorkspaceQuotas.all_objects.filter(  # tenancy: unscoped — quota by PK, armed ws
            workspace_id=ws.id
        ).first()
        if quota is None:
            raise NotFoundError()
        # Usage metering is Phase 11 — unmetered counters report 0 (api-spec §4.4).
        body = {
            "workspace_id": str(ws.id),
            "plan": ws.plan,
            "quotas": {
                "workspace_members": {"limit": quota.max_members, "used": 0},
                "concurrent_streams": {"limit": quota.max_concurrent_streams, "used": 0},
                "per_stream_tps_cap": {"limit": quota.per_stream_tps_cap},
                "aggregate_tps_cap": {"limit": quota.aggregate_tps_cap, "used": 0},
                "events_per_day": {"limit": quota.events_per_day, "used": 0},
                "buffer_retention_hours": {"limit": quota.buffer_retention_hours},
                "backfill": {
                    "max_simulated_days": quota.backfill_max_days,
                    "max_events": quota.backfill_max_events,
                },
                "api_keys": {"limit": quota.max_api_keys, "used": 0},
                "idle_auto_pause_hours": {"limit": quota.idle_pause_minutes // 60},
            },
        }
        return Response(serializers.QuotaSerializer(body).data)


class AuditLogView(APIView):
    """GET /workspaces/{id}/audit-log — admin only (api-spec §4.14)."""

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: serializers.AuditEntrySerializer(many=True)})
    def get(self, request: Request, workspace_id: str) -> Response:
        workspace, role = _resolve_workspace(request, workspace_id)
        _require_admin(role)
        filters = {
            "action": request.query_params.get("action"),
            "action_prefix": request.query_params.get("action_prefix"),
            "actor_id": request.query_params.get("actor_id"),
        }
        entries = _read_audit_log(workspace_id=workspace.id, **filters)
        data = serializers.AuditEntrySerializer(entries, many=True).data  # type: ignore[arg-type]
        return Response({"data": data, "next_cursor": None})


class KeyInfoView(APIView):
    """GET /auth/key-info — the data-plane API-key probe (phase-doc §27).

    API-key-only: a JWT here is the wrong credential type for this surface, so we
    list only ``ApiKeyAuthentication``. No key (or a JWT) → 401 ``invalid-api-key``
    (the key surface's WWW-Authenticate). A revoked key → 401 within 1 s
    (SEC-KEY-5), which is exactly what the demo's stopwatch step asserts.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: serializers.KeyInfoSerializer})
    def get(self, request: Request) -> Response:
        principal = request.user
        if not isinstance(principal, ApiKeyPrincipal):
            from config.problems import InvalidApiKey

            raise InvalidApiKey()  # JWT/none on the key surface → absent credential
        from tenancy.application.keys import VerifiedKey

        verified = VerifiedKey(
            api_key_id=principal.api_key_id,
            workspace_id=principal.workspace_id,
            scopes=principal.scopes,
        )
        return Response(serializers.KeyInfoSerializer(key_service.key_info(verified)).data)


def _resolve_for_read(request: Request, workspace_id: str, *, required_scope: str) -> Workspace:
    """Resolve a workspace for a JWT-or-Key read (api-spec A-5 dual surfaces).

    JWT: membership in the path workspace (else 404). Key: the path workspace MUST
    equal the key's workspace (W-1) else 404; the scope must cover ``required_scope``
    else 403. Arms the workspace context on success.
    """
    try:
        ws_uuid = uuid.UUID(str(workspace_id))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if principal.workspace_id != ws_uuid:
            raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
        if required_scope not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope=required_scope
            )
        arm_request_workspace(request._request, ws_uuid)
        workspace = Workspace.all_objects.filter(  # tenancy: unscoped — ws by id, key-armed
            id=ws_uuid, deleted_at__isnull=True
        ).first()
        if workspace is None:
            raise NotFoundError()
        return workspace
    workspace, _role = _resolve_workspace(request, workspace_id)
    return workspace


def _read_audit_log(
    *,
    workspace_id: uuid.UUID,
    action: str | None = None,
    action_prefix: str | None = None,
    actor_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read this workspace's audit entries via the Audit app's reader (INV-AUD-4).

    Lazily imported so the two contexts build in parallel; if the Audit app's
    reader is not yet present (isolated build), returns an empty page rather than
    crashing — the integrated build always has it.
    """
    try:
        from audit.application.reader import read_workspace_audit_log
    except ImportError:
        return []
    entries: list[dict[str, Any]] = read_workspace_audit_log(
        workspace_id=workspace_id,
        action=action,
        action_prefix=action_prefix,
        actor_id=actor_id,
    )
    return entries
