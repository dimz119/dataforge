"""Stream Control API views (api-spec §4.8 streams #39-44).

Dual JWT|API-key surfaces (api-spec §2.2 A-5; the access-policy table classifies
each route). Every surface masks foreign-workspace access to 404 (W-1/W-3): an
API key is pinned to its key's workspace, a JWT caller must be a member of the
workspace it names. The workspace context is armed on every authenticated request
so the Class-T scoped manager filters all stream reads/writes.

The Phase-5 surface: create (#39, T1), list (#40), retrieve (#41), and the
idempotent start/stop lifecycle verbs (#43-44). Phase 6 adds the pause/resume verbs
(#45-46, T5/T7) and the live ``PATCH`` mutation (#47, target_tps PIN-3). Chaos/delete
land in later phases. Errors are uniform RFC 9457 (config.problems): foreign/absent
→ 404, quota cap → 403 quota-exceeded, illegal-from-state → 409
invalid-state-transition, an immutable/pinned PATCH field → 400 validation-error,
deprecated pin → 409 conflict.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from drf_spectacular.utils import OpenApiParameter, extend_schema
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
    ServiceUnavailable,
)
from config.schema import page_envelope
from identity.infra.jwt import DataForgeJWTAuthentication
from streams.api import serializers
from streams.application import metering, quotas, services
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
    from streams.application.schema_pins import effective_versions_for_stream

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
        # The effective per-subject schema-version map (schema-registry §10.2,
        # additive Phase 10 field): the materialized pin (from the checkpoint after
        # first start) folded with the highest applied upgrade target; a preview from
        # the pinned manifest before first start. {} when no subject resolves.
        "schema_versions": effective_versions_for_stream(stream),
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
        parameters=[
            OpenApiParameter(
                "workspace_id",
                str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="The owning workspace UUID (W-2); absent → 404.",
            ),
            OpenApiParameter(
                "status",
                str,
                location=OpenApiParameter.QUERY,
                description="Comma list of lifecycle_state values to filter by.",
            ),
            OpenApiParameter(
                "scenario_instance_id",
                str,
                location=OpenApiParameter.QUERY,
                description="Filter to streams of one scenario instance.",
            ),
        ],
        responses={200: page_envelope("StreamPage", serializers.StreamResponseSerializer)},
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
            schema_version_pins=dict(data.get("schema_version_pins") or {}),
            virtual_epoch=vclock.get("virtual_epoch"),
            speed_multiplier=vclock.get("speed_multiplier") or Decimal("1.0"),
            clock_mode="live",  # v1 live mode only (backfill is the datasets resource)
            backfill_days=None,
            # Shard count is pinned at start (immutable, INV-STR-5). The serializer
            # bounds it 1..64 (the ≤ 64 platform shards/stream cap, scaling §5.2);
            # there is no per-workspace shard quota in the MVP, so the platform cap is
            # the only ceiling. Defaults to 1 (the single-shard layout).
            shard_count=int(data.get("shard_count", 1)),
        )
        try:
            stream = services.create_stream(
                workspace=workspace, data=create_input, actor=request.user
            )
        except services.PinDeprecated as exc:
            raise ConflictError(str(exc)) from exc
        except services.StreamCreationForbidden as exc:
            raise PermissionDeniedError(str(exc)) from exc
        # PinValidationFailed (PIN-R3) is a ProblemException → rendered directly by the
        # global handler as 422 validation-error with the errors[] extension; no catch.
        response = _response(stream, status_code=status.HTTP_201_CREATED)
        response["Location"] = f"/api/v1/streams/{stream.id}"
        return response


# The only fields a PATCH may mutate (api-spec §4.8.2; PIN-3). Everything else on a
# stream is pinned (PIN-4) — patching it → 400 validation-error "immutable_field".
_PATCH_MUTABLE_FIELDS = frozenset({"name", "target_tps"})


class StreamDetailView(APIView):
    """GET | PATCH /streams/{stream_id} (api-spec §4.8 #41, §4.8.2 #47).

    A flat single-resource route (W-2): the workspace is resolved from the resource.
    The scoped manager masks a foreign stream to no-row → 404 — but the context must
    be armed first. We resolve the stream's workspace via the unscoped manager by its
    unique id, then verify the caller may see that workspace (foreign → 404), arm it,
    and re-read through the scoped manager.

    ``PATCH`` is the live mutation surface (§4.8.2): ``name``/``target_tps`` only
    (PIN-3). ``target_tps`` is bounded 1..1,000 (out of range → 400) and quota-capped
    at command time (above the plan per-stream cap → 403, INV-TEN-5); the runner picks
    it up within 2 s. Any other body key names a pinned field (PIN-4) → 400
    validation-error with ``errors[0].code = "immutable_field"``.
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

    @extend_schema(
        operation_id="streams_patch",
        request=serializers.StreamPatchSerializer,
        responses={200: serializers.StreamResponseSerializer},
    )
    def patch(self, request: Request, stream_id: str) -> Response:
        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=True)
        _reject_immutable_keys(request)
        serializer = serializers.StreamPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if "target_tps" in data:
            try:
                stream = services.request_set_target_tps(
                    stream=stream, target_tps=int(data["target_tps"]), actor=request.user
                )
            except services.StreamQuotaExceeded as exc:
                raise QuotaExceeded(
                    str(exc), quota=exc.quota, limit=exc.limit, requested=exc.requested
                ) from exc
            except metering.AdmissionDenied as exc:
                raise ServiceUnavailable(str(exc), retry_after=exc.retry_after) from exc
        if "name" in data:
            stream = services.request_rename(
                stream=stream, name=str(data["name"]), actor=request.user
            )
        return _response(stream)


def _reject_immutable_keys(request: Request) -> None:
    """Reject a body key naming a pinned field (PIN-4) → 400 immutable_field.

    The merge-patch body may carry only ``name``/``target_tps``; any other key is a
    pinned field a new stream is required for (manifest_version, seed, pinned_config,
    virtual_clock, …). Masked before serializer validation so the contract code
    ``immutable_field`` is surfaced (api-spec §4.8.2).
    """
    from rest_framework.exceptions import ErrorDetail
    from rest_framework.serializers import ValidationError

    body = request.data if isinstance(request.data, dict) else {}
    pinned = [k for k in body if k not in _PATCH_MUTABLE_FIELDS]
    if pinned:
        field = pinned[0]
        raise ValidationError(
            {
                field: ErrorDetail(
                    f"{field!r} is pinned at create and immutable (INV-STR-5); "
                    f"start a new stream to change it.",
                    code="immutable_field",
                )
            }
        )


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


class StreamStatsView(APIView):
    """GET /streams/{stream_id}/stats (api-spec §4.11.1 #55; T-none, Phase 6).

    The tenant-facing StreamStats read: Redis-resident, rebuildable counters
    (``total_events``, ``observed_tps``, ``by_event_type``, ``last_event_at``) with
    staleness ≤ 5 s (INV-OBS-2), workspace-scoped (INV-OBS-3). JWT or API-key
    (``streams:read``); a foreign-workspace credential masks to 404 (W-1/W-3) via the
    shared resolver. ``health`` is derived from the runner lease (heartbeat-fresh
    ≤ 15 s) + the counters' age (§4.11.1).
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_stats",
        responses={200: serializers.StreamStatsResponseSerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        from delivery.application.stream_stats_service import (
            StreamControlFacts,
            build_stream_stats,
        )
        from streams.infra.leases import has_live_lease

        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=False)
        # health: a live lease on ANY shard means the runner heartbeat is fresh (§8.2
        # SET PX 15000 ⇒ the key's existence is the ≤ 15 s freshness signal, §4.11.1).
        runner_alive = any(
            has_live_lease(stream.id, shard_id)
            for shard_id in range(max(1, stream.shard_count))
        )
        facts = StreamControlFacts(
            stream_id=str(stream.id),
            status=stream.status,
            lifecycle_state=stream.lifecycle_state,
            target_tps=stream.target_tps,
            # virtual_now is the runner-advanced live position (Observation-served);
            # not surfaced through the control-plane row in Phase 6 → null (mirrors
            # the Stream resource's virtual_clock.virtual_now).
            virtual_now=None,
            speed_multiplier=float(stream.speed_multiplier),
            runner_alive=runner_alive,
        )
        body = build_stream_stats(workspace_id=str(stream.workspace_id), facts=facts)
        return Response(
            serializers.StreamStatsResponseSerializer(body).data, status=status.HTTP_200_OK
        )


def _serialize_upgrade(stream_id: uuid.UUID, entry: dict[str, Any]) -> dict[str, Any]:
    """The §4.8.4 schema-upgrade resource dict from a persisted jsonb entry.

    The entry is the storage shape (streams.application.schema_upgrades); the wire
    resource adds ``stream_id`` and surfaces the lifecycle members. Optional members
    (``applied_at_wall``/``applied_sequence_no``/``cancelled_at``) are ``None`` until
    the runner cutover (or the cancel path) writes them.
    """
    return {
        "upgrade_id": entry["upgrade_id"],
        "stream_id": stream_id,
        "subject": entry["subject"],
        "target_version": entry["target_version"],
        "at": entry.get("at"),
        "status": entry["status"],
        "created_at": entry["created_at"],
        "applied_at_wall": entry.get("applied_at_wall"),
        "applied_sequence_no": entry.get("applied_sequence_no"),
        "cancelled_at": entry.get("cancelled_at"),
    }


class StreamSchemaUpgradeCollectionView(APIView):
    """POST | GET /streams/{stream_id}/schema-upgrades (api-spec §4.8.4 #50-51).

    POST schedules a mid-stream additive evolution (REG-U001..U007 validation →
    409 ``conflict`` with the ``errors[]`` extension on failure); ``streams:write``.
    Idempotency-Key (I-1): a repeat with the same key returns the already-scheduled
    entry (still 201, no duplicate). GET lists every entry
    (``scheduled``/``applied``/``cancelled``, the cancelled retained); ``streams:read``.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_schema_upgrades_list",
        responses={
            200: page_envelope(
                "StreamSchemaUpgradePage", serializers.SchemaUpgradeResponseSerializer
            )
        },
    )
    def get(self, request: Request, stream_id: str) -> Response:
        from streams.application import schema_upgrades

        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=False)
        entries = schema_upgrades.list_upgrades(stream)
        data = [
            serializers.SchemaUpgradeResponseSerializer(
                _serialize_upgrade(stream.id, e)
            ).data
            for e in entries
        ]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="streams_schema_upgrades_create",
        request=serializers.SchemaUpgradeCreateSerializer,
        responses={201: serializers.SchemaUpgradeResponseSerializer},
    )
    def post(self, request: Request, stream_id: str) -> Response:
        from streams.application import schema_upgrades

        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=True)
        serializer = serializers.SchemaUpgradeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            result = schema_upgrades.schedule_upgrade(
                stream=stream,
                subject_name=str(data["subject"]),
                target_version=int(data["target_version"]),
                at=data.get("at"),
                actor=request.user,
                idempotency_key=idempotency_key,
            )
        except schema_upgrades.UpgradeValidationFailed as exc:
            raise ConflictError(
                "The schema upgrade conflicts with the stream's current state.",
                extensions={"errors": [e.to_dict() for e in exc.errors]},
            ) from exc
        body = serializers.SchemaUpgradeResponseSerializer(
            _serialize_upgrade(stream.id, result.entry)
        ).data
        return Response(body, status=status.HTTP_201_CREATED)


class StreamSchemaUpgradeDetailView(APIView):
    """DELETE /streams/{stream_id}/schema-upgrades/{upgrade_id} (api-spec §4.8.4 #52).

    Cancels a ``scheduled`` entry (→ 204); a non-``scheduled`` entry → 409
    ``invalid-state-transition``; an unknown id → 404. The cancelled entry is retained
    in the list (irreversible history is the audit posture, §10.3). ``streams:write``.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_schema_upgrades_cancel",
        request=None,
        responses={204: None},
    )
    def delete(self, request: Request, stream_id: str, upgrade_id: str) -> Response:
        from streams.application import schema_upgrades

        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=True)
        try:
            schema_upgrades.cancel_upgrade(
                stream=stream, upgrade_id=str(_uuid(upgrade_id)), actor=request.user
            )
        except schema_upgrades.UpgradeNotFound as exc:
            raise NotFoundError() from exc
        except schema_upgrades.UpgradeNotCancellable as exc:
            raise InvalidStateTransition(
                "Only a scheduled upgrade can be cancelled."
            ) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)


class StreamSchemaVersionsView(APIView):
    """GET /streams/{stream_id}/schema-versions (schema-registry §10.2; #?).

    The per-stream effective schema-version projection ``{effective, pending,
    applied}``: ``effective`` the §10.2 ``max(materialized pin, highest applied
    upgrade target)`` map per subject (the materialized pin lives in the checkpoint
    after first start; a preview from the pinned manifest before it); ``pending`` the
    ``scheduled`` upgrade entries awaiting their simulated-time cutover; ``applied``
    the applied entries (with ``applied_at_wall``/``applied_sequence_no``). Cancelled
    entries are surfaced only on the upgrade-list endpoint. ``streams:read``; a
    foreign-workspace credential masks to 404 (W-1/W-3) via the shared resolver.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_schema_versions",
        responses={200: serializers.StreamSchemaVersionsResponseSerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        from streams.application.schema_pins import schema_versions_view_for_stream

        stream = _resolve_stream_for_principal(request, _uuid(stream_id), write=False)
        view = schema_versions_view_for_stream(stream)
        body = {
            "effective": view["effective"],
            "pending": [_serialize_upgrade(stream.id, e) for e in view["pending"]],
            "applied": [_serialize_upgrade(stream.id, e) for e in view["applied"]],
        }
        return Response(
            serializers.StreamSchemaVersionsResponseSerializer(body).data,
            status=status.HTTP_200_OK,
        )


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
        except metering.AdmissionDenied as exc:
            raise ServiceUnavailable(str(exc), retry_after=exc.retry_after) from exc
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


class StreamPauseView(_LifecycleVerbView):
    """POST /streams/{stream_id}/pause (api-spec §4.8.1 #45; T5).

    Idempotent (INV-STR-3): pause on a paused-desired stream is a no-op returning
    current state. Guarded: pause is legal only from a live lifecycle
    (``running``/``starting``/``resuming``) — else 409 invalid-state-transition (T5).
    Always an explicit user pause here (``status_reason = user``); the quota/idle
    system-pause TRIGGERS are Phase 11 (the lifecycle Celery handler calls
    ``request_pause(reason=...)`` directly, not this route).
    """

    @extend_schema(
        operation_id="streams_pause",
        request=None,
        responses={200: serializers.StreamResponseSerializer},
    )
    def post(self, request: Request, stream_id: str) -> Response:
        stream = self._resolve(request, stream_id)
        try:
            stream = services.request_pause(stream=stream, actor=request.user)
        except services.StreamNotPausable as exc:
            raise InvalidStateTransition(str(exc)) from exc
        return _response(stream)


class StreamResumeView(_LifecycleVerbView):
    """POST /streams/{stream_id}/resume (api-spec §4.8.1 #46; T7).

    Idempotent (INV-STR-3): resume on a running-desired stream is a no-op. Guarded:
    resume is legal only from ``paused``/``pausing`` — else 409. If the pause reason
    was ``quota`` (Phase 11), restored headroom is required at command time
    (INV-TEN-5) → else 403 quota-exceeded (T7).
    """

    @extend_schema(
        operation_id="streams_resume",
        request=None,
        responses={200: serializers.StreamResponseSerializer},
    )
    def post(self, request: Request, stream_id: str) -> Response:
        stream = self._resolve(request, stream_id)
        try:
            stream = services.request_resume(stream=stream, actor=request.user)
        except services.StreamQuotaExceeded as exc:
            raise QuotaExceeded(
                str(exc), quota=exc.quota, limit=exc.limit, requested=exc.requested
            ) from exc
        except services.StreamNotResumable as exc:
            raise InvalidStateTransition(str(exc)) from exc
        return _response(stream)


