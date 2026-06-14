"""Stream Control API views (api-spec §4.8 streams #39-44).

Dual JWT|API-key surfaces (api-spec §2.2 A-5; the access-policy table classifies
each route). Every surface masks foreign-workspace access to 404 (W-1/W-3): an
API key is pinned to its key's workspace, a JWT caller must be a member of the
workspace it names. The workspace context is armed on every authenticated request
so the Class-T scoped manager filters all stream reads/writes.

The Phase-5 surface: create (#39, T1), list (#40), retrieve (#41), and the
idempotent start/stop lifecycle verbs (#43-44). Pause/resume/PATCH/chaos/delete
land in later phases (their service handlers exist; the routes are mounted as those
phases land). Errors are uniform RFC 9457 (config.problems): foreign/absent → 404,
quota cap → 403 quota-exceeded, illegal-from-state → 409 invalid-state-transition,
deprecated pin → 409 conflict.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import (
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceeded,
)
from identity.infra.jwt import DataForgeJWTAuthentication
from streams.api import serializers
from streams.application import quotas, services
from streams.domain.models import Stream
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace

# The API-key scopes the stream surfaces require (api-spec §4.8 #39-44 / A-4).
_SCOPE_WRITE = "streams:write"
_SCOPE_READ = "streams:read"


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc


def _user(request: Request) -> Any:
    """The authenticated principal (User on JWT surfaces; never an ApiKeyPrincipal)."""
    return request.user


def _serialize_stream(stream: Stream) -> dict[str, Any]:
    """The §4.8 Stream resource dict (seed rendered as a string, PIN-1)."""
    return {
        "stream_id": stream.id,
        "workspace_id": stream.workspace_id,
        "scenario_instance_id": stream.scenario_config_id,
        "name": stream.name,
        "scenario_slug": stream.scenario_slug,
        "manifest_version": stream.manifest_version,
        "config_revision": stream.pinned_config_version,
        "pin_sha256": stream.pin_sha256,
        "seed": str(stream.seed),
        "status": stream.status,
        "status_reason": stream.status_reason,
        "desired_state": {
            "run_state": stream.desired_state,
            "target_tps": stream.target_tps,
        },
        "virtual_clock": {
            "virtual_epoch": stream.virtual_epoch,
            "speed_multiplier": stream.speed_multiplier,
            # virtual_now (live simulated position) is an Observation-served value
            # in Phase 6; null here (the clock is pinned, the runner advances it).
            "virtual_now": None,
        },
        "shard_count": stream.shard_count,
        "created_at": stream.created_at,
        "started_at": stream.first_started_at,
        "last_transition_at": stream.last_transition_at,
    }


def _response(stream: Stream, *, status_code: int = status.HTTP_200_OK) -> Response:
    body = serializers.StreamResponseSerializer(_serialize_stream(stream)).data
    return Response(body, status=status_code)


def _require_member(request: Request, workspace_id: uuid.UUID, *, verified: bool = False) -> None:
    """The JWT caller must be a member of ``workspace_id`` (foreign → 404, W-3)."""
    from tenancy.application import services as tenancy_services

    membership = tenancy_services.get_membership(workspace_id, _user(request))
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()
    if verified and not getattr(request.user, "is_verified", False):
        from config.problems import EmailNotVerified

        raise EmailNotVerified()


def _resolve_ws_for_write(request: Request, workspace_id: uuid.UUID) -> Any:
    """Resolve + arm a workspace for a JWT-or-Key write (foreign → 404).

    Key: the workspace MUST equal the key's workspace (W-1) and the key needs
    ``streams:write``. JWT: verified membership in the workspace.
    """
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if principal.workspace_id != workspace_id:
            raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
        if _SCOPE_WRITE not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope=_SCOPE_WRITE
            )
        arm_request_workspace(request._request, workspace_id)
        return _live_workspace(workspace_id)
    _require_member(request, workspace_id, verified=True)
    arm_request_workspace(request._request, workspace_id)
    return _live_workspace(workspace_id)


def _arm_for_read(request: Request, workspace_id: uuid.UUID) -> None:
    """Arm the workspace context for a JWT-or-Key read (foreign → 404).

    Key: workspace must match the key's (W-1) + ``streams:read``. JWT: membership.
    """
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if principal.workspace_id != workspace_id:
            raise NotFoundError()
        if _SCOPE_READ not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope=_SCOPE_READ
            )
        arm_request_workspace(request._request, workspace_id)
        return
    _require_member(request, workspace_id)
    arm_request_workspace(request._request, workspace_id)


def _live_workspace(workspace_id: uuid.UUID) -> Any:
    from tenancy.domain.models import Workspace

    workspace = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
    if workspace is None:
        raise NotFoundError()
    return workspace


def _query_workspace_id(request: Request) -> uuid.UUID:
    """The query ``workspace_id`` for a flat collection GET (W-2). Absent → 404.

    Every flat route names its owning workspace explicitly (no implicit "the key's
    workspace") so a foreign credential without (or with a foreign) workspace_id
    masks to 404 uniformly across JWT and key — the SCOPE cross-tenant contract
    (mirrors the datasets surface).
    """
    raw = request.query_params.get("workspace_id")
    if not raw:
        raise NotFoundError()  # collection route requires workspace_id (W-2 / W-3)
    return _uuid(raw)


def _body_workspace_id(request: Request) -> uuid.UUID:
    """The body ``workspace_id`` for a flat collection POST. Absent/malformed → 404."""
    raw = request.data.get("workspace_id") if isinstance(request.data, dict) else None
    if not raw:
        raise NotFoundError()  # masks before serializer validation (W-3)
    return _uuid(str(raw))


class StreamCollectionView(APIView):
    """GET/POST /streams (api-spec §4.8 #39-40)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_list",
        responses={200: serializers.StreamResponseSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        workspace_id = _query_workspace_id(request)
        _arm_for_read(request, workspace_id)
        rows = Stream.objects.all().order_by("-created_at")
        status_filter = request.query_params.get("status")
        if status_filter:
            wanted = {s.strip() for s in status_filter.split(",") if s.strip()}
            rows = rows.filter(lifecycle_state__in=wanted)
        instance_filter = request.query_params.get("scenario_instance_id")
        if instance_filter:
            rows = rows.filter(scenario_config_id=_uuid(instance_filter))
        data = [serializers.StreamResponseSerializer(_serialize_stream(s)).data for s in rows]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="streams_create",
        request=serializers.StreamCreateSerializer,
        responses={201: serializers.StreamResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        # Resolve + mask the owning workspace BEFORE serializer validation so a
        # foreign credential gets 404 (W-3), not a 400 that confirms the route shape.
        workspace_id = _body_workspace_id(request)
        workspace = _resolve_ws_for_write(request, workspace_id)
        serializer = serializers.StreamCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        # Per-stream TPS cap (PIN-3): the v1 request bound is 1..1000 (serializer);
        # the plan ceiling is a 403 quota check at create.
        target_tps = int(data["target_tps"])
        if target_tps > quotas.per_stream_tps_cap(workspace_id):
            raise QuotaExceeded(
                "The requested target_tps exceeds your plan's per-stream cap.",
                quota="per_stream_tps",
                limit=quotas.per_stream_tps_cap(workspace_id),
                requested=target_tps,
            )
        vclock = dict(data.get("virtual_clock") or {})
        create_input = services.StreamCreateInput(
            name=data["name"],
            scenario_instance_id=data["scenario_instance_id"],
            seed=data.get("seed"),
            target_tps=target_tps,
            chaos_config=dict(data.get("chaos") or {}),
            virtual_epoch=vclock.get("virtual_epoch"),
            speed_multiplier=vclock.get("speed_multiplier") or Decimal("1.0"),
            clock_mode="live",  # v1 live mode only (backfill is the datasets resource)
            backfill_days=None,
        )
        try:
            stream = services.create_stream(
                workspace=workspace, data=create_input, actor=request.user
            )
        except services.PinDeprecated as exc:
            raise ConflictError(str(exc)) from exc
        response = _response(stream, status_code=status.HTTP_201_CREATED)
        response["Location"] = f"/api/v1/streams/{stream.id}"
        return response


class StreamDetailView(APIView):
    """GET /streams/{stream_id} (api-spec §4.8 #41).

    A flat single-resource route (W-2): the workspace is resolved from the resource.
    The scoped manager masks a foreign stream to no-row → 404 — but the context must
    be armed first. We resolve the stream's workspace via the unscoped manager by its
    unique id, then verify the caller may see that workspace (foreign → 404), arm it,
    and re-read through the scoped manager.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_retrieve",
        responses={200: serializers.StreamResponseSerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=False)
        return _response(stream)


def _resolve_stream_for_principal(
    request: Request, stream_id: uuid.UUID, *, write: bool
) -> Stream:
    """Resolve a stream by id, masking foreign access to 404 (W-1/W-3).

    Reads the stream by unique id (unscoped), derives its workspace, checks the
    caller may access that workspace (key match + scope, or JWT membership), arms the
    context, and returns the row. A foreign workspace masks to 404 before any state
    is revealed.
    """
    # tenancy: unscoped — single-resource route resolves the owning workspace from
    # the unique id, then re-checks access + arms the scoped context (W-2). The
    # pre-arm read runs under platform_read_scope so the strict Class T RLS policy
    # admits the row to the NOBYPASSRLS runtime role before any workspace is armed
    # (backend-architecture §4.2; read-only — foreign access is still masked to 404
    # by the access check below + the scoped re-read).
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        row = Stream.all_objects.filter(id=stream_id).first()
    if row is None:
        raise NotFoundError()
    if write:
        _resolve_ws_for_write(request, row.workspace_id)
    else:
        _arm_for_read(request, row.workspace_id)
    scoped: Stream | None = Stream.objects.filter(id=stream_id).first()
    if scoped is None:
        raise NotFoundError()
    return scoped


class _LifecycleVerbView(APIView):
    """Base for the idempotent lifecycle verbs (api-spec §4.8.1 #43-44).

    Each verb resolves the stream (write access → 404 foreign), calls one service
    handler, and returns 200 with the current resource. The handlers are idempotent
    (INV-STR-3): re-issuing the current desired state is a no-op returning current
    state.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def _resolve(self, request: Request, stream_id: str) -> Stream:
        return _resolve_stream_for_principal(request, _uuid(stream_id), write=True)


class StreamStartView(_LifecycleVerbView):
    """POST /streams/{stream_id}/start (api-spec §4.8.1 #43; T2/T12/T13)."""

    @extend_schema(
        operation_id="streams_start",
        request=None,
        responses={200: serializers.StreamResponseSerializer},
    )
    def post(self, request: Request, stream_id: str) -> Response:
        stream = self._resolve(request, stream_id)
        try:
            stream = services.request_start(stream=stream, actor=request.user)
        except services.StreamQuotaExceeded as exc:
            raise QuotaExceeded(
                str(exc), quota=exc.quota, limit=exc.limit, requested=exc.requested
            ) from exc
        except services.StreamNotStartable as exc:
            raise InvalidStateTransition(str(exc)) from exc
        return _response(stream)


class StreamStopView(_LifecycleVerbView):
    """POST /streams/{stream_id}/stop (api-spec §4.8.1 #44; T9/T10)."""

    @extend_schema(
        operation_id="streams_stop",
        request=None,
        responses={200: serializers.StreamResponseSerializer},
    )
    def post(self, request: Request, stream_id: str) -> Response:
        stream = self._resolve(request, stream_id)
        stream = services.request_stop(stream=stream, actor=request.user)
        return _response(stream)
