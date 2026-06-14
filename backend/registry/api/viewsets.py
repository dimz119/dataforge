"""Schema Registry read-API views (api-spec §4.12 #62-65; schema-registry §7).

Read-only over the in-house registry: writes happen only through manifest
publication (R-DER) — there is **no** registration endpoint in /api/v1 (the
explicit-evolution command is the Flow-2 write path, Phase 10). Auth is console
JWT (any workspace member) or an API key with ``schemas:read`` (A-4); the
key-authenticated reads draw from the ``data-events`` bucket (api-spec §2.8).

Global-scenario subjects are readable by any authenticated principal; workspace-
scenario subjects only within their workspace (404 masking outside it, W-3). The
view arms the caller's workspace context so the hybrid Class-H RLS admits the
caller's own subjects alongside globals (registry.infra.rls). Subject names
contain dots and are used verbatim as path segments.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import NotFoundError, PermissionDeniedError
from identity.infra.jwt import DataForgeJWTAuthentication
from registry.api import serializers
from registry.application import services
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace

_READ_SCOPE = "schemas:read"


def _caller_workspace_id(request: Request) -> uuid.UUID | None:
    """The workspace whose own subjects the caller may also see (globals always).

    Key: pinned to the key's workspace, and the key must carry ``schemas:read``.
    JWT: the caller's first/active membership context — registry reads are not
    path-scoped, so a JWT caller sees globals plus subjects of the workspaces they
    belong to. Arms the workspace context so the Class-H RLS admits those rows.
    """
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if _READ_SCOPE not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope=_READ_SCOPE
            )
        arm_request_workspace(request._request, principal.workspace_id)
        return principal.workspace_id
    requested = _query_workspace_id(request)
    if requested is None:
        return None
    from tenancy.application import services as tenancy_services

    membership = tenancy_services.get_membership(requested, cast(Any, principal))
    if membership is None:
        raise NotFoundError()
    arm_request_workspace(request._request, requested)
    return requested


def _query_workspace_id(request: Request) -> uuid.UUID | None:
    raw = request.query_params.get("workspace_id")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc


def _serialize_summary(summary: services.SubjectSummary) -> dict[str, Any]:
    return {
        "subject": summary.subject.subject,
        "scenario_slug": summary.scenario_slug,
        "compatibility": summary.subject.compatibility_mode,
        "latest_version": summary.latest_version,
        "versions": summary.version_numbers,
    }


def _serialize_provenance(version: Any) -> dict[str, Any]:
    return {
        "version": version.version,
        "registered_at": version.registered_at,
        "manifest_version": services.manifest_version_for(version),
    }


class SchemaCollectionView(APIView):
    """GET /schemas (api-spec §4.12 #62)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="schemas_list",
        responses={200: serializers.SubjectSummarySerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        workspace_id = _caller_workspace_id(request)
        scenario_slug = request.query_params.get("scenario_slug")
        summaries = services.list_subjects(
            workspace_id=workspace_id, scenario_slug=scenario_slug
        )
        data = [
            serializers.SubjectSummarySerializer(_serialize_summary(s)).data for s in summaries
        ]
        return Response({"data": data, "next_cursor": None})


class SchemaDetailView(APIView):
    """GET /schemas/{subject} (api-spec §4.12 #63)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="schemas_retrieve",
        responses={200: serializers.SubjectDetailSerializer},
    )
    def get(self, request: Request, subject: str) -> Response:
        workspace_id = _caller_workspace_id(request)
        summary = services.get_subject(subject, workspace_id=workspace_id)
        if summary is None:
            raise NotFoundError()
        body = _serialize_summary(summary)
        body["created_at"] = summary.subject.created_at
        body["version_provenance"] = [_serialize_provenance(v) for v in summary.versions]
        return Response(serializers.SubjectDetailSerializer(body).data)


class SchemaVersionsView(APIView):
    """GET /schemas/{subject}/versions (api-spec §4.12 #64)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="schemas_versions_list",
        responses={200: serializers.VersionProvenanceSerializer(many=True)},
    )
    def get(self, request: Request, subject: str) -> Response:
        workspace_id = _caller_workspace_id(request)
        summary = services.get_subject(subject, workspace_id=workspace_id)
        if summary is None:
            raise NotFoundError()
        data = [_serialize_provenance(v) for v in summary.versions]
        return Response({"data": data, "next_cursor": None})


class SchemaVersionDetailView(APIView):
    """GET /schemas/{subject}/versions/{version} (api-spec §4.12 #65).

    ``{version}`` is an integer or the literal ``latest``. The ``schema`` member is
    the stored document verbatim (annotations included) — what consumer pipelines
    fetch to resolve a ``schema_ref`` (exercise E5).
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="schemas_version_retrieve",
        responses={200: serializers.VersionRecordSerializer},
    )
    def get(self, request: Request, subject: str, schema_version: str) -> Response:
        workspace_id = _caller_workspace_id(request)
        record = services.get_version(subject, schema_version, workspace_id=workspace_id)
        if record is None:
            raise NotFoundError()
        body = {
            "subject": record.subject.subject,
            "version": record.version,
            "manifest_version": services.manifest_version_for(record),
            "registered_at": record.registered_at,
            "schema": record.json_schema,
        }
        return Response(serializers.VersionRecordSerializer(body).data)
