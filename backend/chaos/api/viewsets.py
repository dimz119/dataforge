"""DRF viewsets for the Chaos context (backend-architecture §3.1, api layer).

Two surfaces, both flat single-resource routes under a stream id (W-2); both
resolve the stream's owning workspace from its unique id, mask foreign access to
404 (W-1/W-3) before any state is revealed, then arm the scoped workspace context
so the Class-T managers / RLS filter the reads and writes.

* ``GET | PATCH /streams/{id}/chaos`` (api-spec §4.8.3) — the live ChaosPolicy
  desired-state surface. PATCH requires ``streams:write`` (API key) or JWT
  membership; the body is validated against the §3.4 pinned bounds
  (``rate ≤ 0.5`` per mode → 422) before it is written to ``chaos_config`` and
  audited ``streams.stream.chaos_policy_changed`` (the runner picks it up next tick).
* ``GET /streams/{id}/answer-key/{injections,summary,export}`` (api-spec §4.13;
  chaos-engine §7.2) — the instructor ground-truth reads over ``chaos_injections``.
  GATED by workspace **admin** (JWT) OR the ``answer_key:read`` API-key scope
  (AK-1): a member / unscoped key in its own workspace → 403; a foreign workspace
  → 404. Every read writes ``chaos.answer_key.accessed`` (AK-3).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.http import StreamingHttpResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from chaos.api import serializers
from chaos.application import answer_key
from chaos.application.validation import (
    ChaosPolicyInvalid,
    validate_chaos_patch,
    validate_drift_arming,
)
from config.problems import (
    CursorInvalid,
    ManifestValidationFailed,
    NotFoundError,
    PermissionDeniedError,
)
from config.schema import page_envelope
from dataforge_engine.chaos import default_policy
from identity.infra.jwt import DataForgeJWTAuthentication
from streams.application import services as stream_services
from streams.domain.models import Stream
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace

# Scopes (api-spec §4.8.3 / §4.13, A-4).
_SCOPE_STREAMS_WRITE = "streams:write"
_SCOPE_STREAMS_READ = "streams:read"
_SCOPE_ANSWER_KEY = "answer_key:read"


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc  # malformed id masks to 404 (W-3)


def _parse_ts(raw: str | None) -> Any:
    if not raw:
        return None
    from django.utils.dateparse import parse_datetime

    return parse_datetime(raw)


def _resolve_stream_workspace(stream_id: uuid.UUID) -> tuple[Stream, uuid.UUID]:
    """Resolve a stream by unique id (unscoped, platform-read), derive its workspace.

    Returns the (pre-arm) row and its workspace id; raises 404 if absent. The caller
    re-confirms visibility through the scoped manager AFTER arming (defense in depth).
    """
    from tenancy.application.services import platform_read_scope

    # tenancy: unscoped — single-resource route resolves the owning workspace from
    # the unique id under platform_read_scope so the strict Class-T RLS policy admits
    # the row to the NOBYPASSRLS runtime role before any workspace is armed; foreign
    # access is still masked to 404 by the access check + scoped re-read.
    with platform_read_scope():
        row = Stream.all_objects.filter(id=stream_id).first()
    if row is None:
        raise NotFoundError()
    return row, uuid.UUID(str(row.workspace_id))


def _scoped_stream(stream_id: uuid.UUID) -> Stream:
    """Re-read the stream through the scoped manager (foreign/absent → 404)."""
    scoped: Stream | None = Stream.objects.filter(id=stream_id).first()
    if scoped is None:
        raise NotFoundError()
    return scoped


def _require_member(request: Request, workspace_id: uuid.UUID) -> Any:
    """The JWT caller must be a member of ``workspace_id`` (foreign → 404, W-3)."""
    from typing import cast

    from identity.domain.models import User
    from tenancy.application import services as tenancy_services

    user = cast("User", request.user)
    membership = tenancy_services.get_membership(workspace_id, user)
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()
    return membership


class StreamChaosView(APIView):
    """GET | PATCH /streams/{stream_id}/chaos (api-spec §4.8.3; chaos-engine §3.5)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def _serialize(self, stream: Stream) -> dict[str, Any]:
        # The live policy = stored chaos_config merged over the disabled defaults, so
        # the response is always the closed seven-mode shape (chaos-engine §3.2).
        modes: dict[str, Any] = dict(default_policy())
        modes.update(dict(stream.chaos_config or {}))
        return {
            "stream_id": stream.id,
            "modes": modes,
            "updated_at": stream.updated_at,
        }

    @extend_schema(
        operation_id="streams_chaos_get",
        responses={200: serializers.ChaosPolicyResponseSerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        stream = self._authorize(request, _uuid(stream_id), write=False)
        body = serializers.ChaosPolicyResponseSerializer(self._serialize(stream)).data
        return Response(body)

    @extend_schema(
        operation_id="streams_chaos_patch",
        request=serializers.ChaosPolicyPatchSerializer,
        responses={200: serializers.ChaosPolicyResponseSerializer},
    )
    def patch(self, request: Request, stream_id: str) -> Response:
        stream = self._authorize(request, _uuid(stream_id), write=True)
        patch = request.data if isinstance(request.data, dict) else {}
        try:
            validate_chaos_patch(dict(patch))
            # CH-V07 (schema-registry §11 DR-3): if the PATCH leaves schema_drift
            # enabled, the stream must have a subject with a registered next version
            # above its effective version — else the mode could never draw a field.
            # The effective map (a checkpoint + registry read) is computed lazily, only
            # when drift ends up enabled, so a non-drift PATCH pays nothing for it.
            merged = self._merged_chaos(stream, dict(patch))
            drift = merged.get("schema_drift")
            if isinstance(drift, dict) and drift.get("enabled"):
                validate_drift_arming(
                    resulting_config=merged,
                    effective=self._effective_versions(stream),
                    workspace_id=None,
                )
        except ChaosPolicyInvalid as exc:
            raise ManifestValidationFailed(
                "The chaos policy failed validation.", errors=exc.errors
            ) from exc
        stream = stream_services.request_set_chaos_policy(
            stream=stream, patch=dict(patch), actor=request.user
        )
        body = serializers.ChaosPolicyResponseSerializer(self._serialize(stream)).data
        return Response(body, status=status.HTTP_200_OK)

    @staticmethod
    def _merged_chaos(stream: Stream, patch: dict[str, Any]) -> dict[str, Any]:
        """The resulting chaos document = stored config + PATCH (mode-level merge, §3.5).

        Mirrors :func:`streams.application.services.request_set_chaos_policy`'s merge so
        the CH-V07 arming check sees exactly what would be stored (each present key
        replaces wholesale; absent keys are untouched). Used only to decide whether
        ``schema_drift`` ends up enabled — the actual write still goes through the
        service.
        """
        merged = dict(stream.chaos_config or {})
        merged.update(patch)
        return merged

    @staticmethod
    def _effective_versions(stream: Stream) -> dict[str, int]:
        """The stream's §10.2 effective ``{subject: version}`` map for the CH-V07 check.

        Folds the materialized pin (PIN-R1/R2) with the highest applied upgrade target
        (§10.2). After first start the materialized map lives in the checkpoint; before
        it (a stream armed with drift at create / before its first tick) the map is
        resolved on the fly with :func:`materialize_pins` against the pinned manifest —
        so an explicit ``{subject: 1}`` pin with v2 registered is eligible immediately
        (the E5 exercise arms drift up front), while a default-latest pin is correctly
        ineligible (effective = latest, nothing above it).
        """
        from streams.application.schema_pins import (
            applied_from_checkpoint,
            effective_versions,
            materialize_pins,
            materialized_from_checkpoint,
        )

        materialized = materialized_from_checkpoint(stream.id)
        if not materialized:
            manifest = dict(stream.pinned_config or {})
            if manifest:
                materialized = materialize_pins(
                    dict(stream.schema_version_pins or {}), manifest=manifest
                )
        applied = applied_from_checkpoint(stream.id)
        return effective_versions(materialized, applied)

    def _authorize(self, request: Request, stream_id: uuid.UUID, *, write: bool) -> Stream:
        """Resolve + arm; foreign → 404, key needs streams:write/read else 403."""
        _, workspace_id = _resolve_stream_workspace(stream_id)
        principal = request.user
        if isinstance(principal, ApiKeyPrincipal):
            if principal.workspace_id != workspace_id:
                raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
            needed = _SCOPE_STREAMS_WRITE if write else _SCOPE_STREAMS_READ
            if needed not in principal.scopes:
                raise PermissionDeniedError(
                    "The API key lacks a required scope.", required_scope=needed
                )
        else:
            _require_member(request, workspace_id)
        arm_request_workspace(request._request, workspace_id)
        return _scoped_stream(stream_id)


class _AnswerKeyView(APIView):
    """Shared answer-key auth (chaos-engine §7.2 AK-1/AK-3/AK-4).

    GATED by workspace **admin** (JWT) OR the ``answer_key:read`` API-key scope. A
    foreign workspace masks to 404 (W-1/W-3) BEFORE the role/scope check, so existence
    is never confirmed; a member / unscoped key WITHIN its own workspace → 403. Every
    successful read audits ``chaos.answer_key.accessed`` (AK-3).
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def _authorize(self, request: Request, stream_id: uuid.UUID) -> Stream:
        from tenancy.domain.models import ROLE_ADMIN

        _, workspace_id = _resolve_stream_workspace(stream_id)
        principal = request.user
        if isinstance(principal, ApiKeyPrincipal):
            if principal.workspace_id != workspace_id:
                raise NotFoundError()  # foreign workspace masked first (W-1)
            if _SCOPE_ANSWER_KEY not in principal.scopes:
                raise PermissionDeniedError(
                    "The API key lacks the answer_key:read scope.",
                    required_scope=_SCOPE_ANSWER_KEY,
                )
        else:
            membership = _require_member(request, workspace_id)  # foreign → 404
            if membership.role != ROLE_ADMIN:
                # A member within its own workspace is forbidden (AK-1), not masked:
                # membership confirms the workspace is the caller's own.
                raise PermissionDeniedError(
                    "The answer key is readable by workspace admins only."
                )
        # Arm AFTER access checks so injection reads are RLS-scoped (AK-4).
        arm_request_workspace(request._request, workspace_id)
        return _scoped_stream(stream_id)

    def _audit(self, request: Request, stream: Stream, *, filters: dict[str, Any]) -> None:
        from streams.application import audit

        audit.emit(
            "chaos.answer_key.accessed",
            actor=request.user,
            workspace_id=stream.workspace_id,
            target={"type": "stream", "id": str(stream.id), "label": stream.name},
            metadata={"filters": filters},
        )

    def _filters(self, request: Request) -> tuple[answer_key.InjectionFilters, dict[str, Any]]:
        params = request.query_params
        mode = params.get("mode") or None
        event_id = params.get("event_id") or None
        from_ts = _parse_ts(params.get("from"))
        to_ts = _parse_ts(params.get("to"))
        filters = answer_key.InjectionFilters(
            mode=mode, event_id=event_id, from_ts=from_ts, to_ts=to_ts
        )
        audit_filters = {
            "mode": mode,
            "event_id": event_id,
            "from": params.get("from"),
            "to": params.get("to"),
        }
        return filters, audit_filters


_ANSWER_KEY_FILTER_PARAMS = [
    OpenApiParameter("mode", str, description="One of the seven ChaosMode identifiers."),
    OpenApiParameter("event_id", str, description="Exact affected canonical event id."),
    OpenApiParameter("from", str, description="RFC-3339 lower bound on occurred_at."),
    OpenApiParameter("to", str, description="RFC-3339 upper bound on occurred_at."),
]


class AnswerKeyInjectionsView(_AnswerKeyView):
    """GET /streams/{id}/answer-key/injections (api-spec §4.13; chaos-engine §7.3)."""

    @extend_schema(
        operation_id="streams_answer_key_injections",
        parameters=[
            *_ANSWER_KEY_FILTER_PARAMS,
            OpenApiParameter("cursor", str, description="Opaque resume cursor (P-1)."),
            OpenApiParameter("limit", int, description="Page size 1..500 (default 100)."),
        ],
        responses={
            200: page_envelope(
                "AnswerKeyInjectionsPage", serializers.AnswerKeyInjectionSerializer
            )
        },
    )
    def get(self, request: Request, stream_id: str) -> Response:
        stream = self._authorize(request, _uuid(stream_id))
        filters, audit_filters = self._filters(request)
        cursor = request.query_params.get("cursor")
        limit = request.query_params.get("limit") or answer_key.DEFAULT_LIMIT
        try:
            page = answer_key.list_injections(
                stream_id=str(stream.id), filters=filters, cursor=cursor, limit=int(limit)
            )
        except answer_key.InvalidCursor as exc:
            raise CursorInvalid(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise CursorInvalid("limit must be an integer.") from exc
        self._audit(request, stream, filters=audit_filters)
        return Response({"data": page.data, "next_cursor": page.next_cursor})


class AnswerKeySummaryView(_AnswerKeyView):
    """GET /streams/{id}/answer-key/summary (api-spec §4.13; chaos-engine §7.3)."""

    @extend_schema(
        operation_id="streams_answer_key_summary",
        parameters=_ANSWER_KEY_FILTER_PARAMS,
        responses={200: serializers.AnswerKeySummarySerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        from django.utils import timezone

        stream = self._authorize(request, _uuid(stream_id))
        filters, audit_filters = self._filters(request)
        summary = answer_key.summarize(stream_id=str(stream.id), filters=filters)
        self._audit(request, stream, filters=audit_filters)
        body = {
            "stream_id": stream.id,
            "window": {"from": filters.from_ts, "to": filters.to_ts},
            "by_mode": summary["by_mode"],
            "total_injections": summary["total_injections"],
            "as_of": timezone.now(),
        }
        return Response(serializers.AnswerKeySummarySerializer(body).data)


class AnswerKeyExportView(_AnswerKeyView):
    """GET /streams/{id}/answer-key/export (JSONL export of injection records).

    Streams the filtered ``chaos_injections`` records as JSONL
    (``application/x-ndjson``), one flattened record per line, newest-first — the bulk
    counterpart of the paginated injections list for offline grading. Same auth, same
    audit (``chaos.answer_key.accessed``) as the other answer-key reads.
    """

    @extend_schema(
        operation_id="streams_answer_key_export",
        parameters=_ANSWER_KEY_FILTER_PARAMS,
        responses={200: None},
    )
    def get(self, request: Request, stream_id: str) -> StreamingHttpResponse:
        stream = self._authorize(request, _uuid(stream_id))
        filters, audit_filters = self._filters(request)
        self._audit(request, stream, filters=audit_filters)
        stream_id_str = str(stream.id)
        # Materialize the JSONL lines NOW, while the workspace context is armed — the
        # scoped manager is fail-closed (security §4.1) and the contextvar is torn down
        # once the request returns, so a lazily-evaluated streaming queryset would read
        # outside the armed context. The export is retention-bounded, so eager
        # materialization is safe; the response still streams the buffered lines.
        lines = list(
            answer_key.iter_injections_jsonl(stream_id=stream_id_str, filters=filters)
        )
        response = StreamingHttpResponse(iter(lines), content_type="application/x-ndjson")
        response["Content-Disposition"] = (
            f'attachment; filename="answer-key-{stream_id_str}.jsonl"'
        )
        return response
