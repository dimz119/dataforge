"""Catalog API views (api-spec §4.6 scenarios #26-32, §4.7 instances #33-38).

Two surfaces with distinct auth shapes (api-spec §2.2 A-5):

* **Scenario reads** (#26-30) — JWT or API key (any scope). They serve global
  (platform-curated) scenarios to every authenticated principal and the caller's
  own workspace scenarios on top. Reads arm the caller's workspace context so the
  hybrid Class-H RLS admits their own rows alongside globals (catalog.infra.rls).
* **Scenario writes** (#31-32) — JWT only (the AI-manifest / admin console). A
  draft is workspace-visibility (§12); publish requires admin of the owning
  workspace.
* **Scenario instances** (#33-38) — JWT or key, path-scoped to ``{workspace_id}``;
  foreign workspace masks to 404 (W-3). Instances are Class-T tenant rows.

Errors are uniform RFC 9457 (config.problems): a manifest/overlay validation
failure → 422 ``manifest-validation-failed`` carrying the §8.3 report; an
unpassed publish → 409 ``conflict``; a foreign/absent resource → 404.
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

from catalog.api import serializers
from catalog.application import ingest, publish, services
from config.problems import (
    ConflictError,
    ManifestValidationFailed,
    NotFoundError,
    PayloadTooLarge,
    PermissionDeniedError,
    RateLimited,
)
from identity.infra.jwt import DataForgeJWTAuthentication
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace
from tenancy.domain.models import ROLE_ADMIN

# B-01 raw-document cap (api-spec §4.6, plugin-arch §9.1): 512 KiB.
_MAX_DOCUMENT_BYTES = 512 * 1024


def _user(request: Request) -> Any:
    """The authenticated User principal (JWT surfaces; never an ApiKeyPrincipal)."""
    return request.user


def _caller_workspace_id(request: Request, requested: uuid.UUID | None) -> uuid.UUID | None:
    """The workspace whose own scenarios the caller may also see.

    An API-key principal is pinned to its key's workspace (ignores any query
    ``workspace_id``); a JWT caller may opt in to a workspace they belong to via
    the ``workspace_id`` query param. Returns ``None`` for a globals-only read.
    Arms the workspace context so the Class-H RLS admits the workspace's rows.
    """
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        arm_request_workspace(request._request, principal.workspace_id)
        return principal.workspace_id
    if requested is None:
        return None
    from tenancy.application import services as tenancy_services

    membership = tenancy_services.get_membership(requested, _user(request))
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()  # not a member of the requested workspace → 404 (W-3)
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


def _serialize_summary(summary: services.ScenarioSummary) -> dict[str, Any]:
    scenario = summary.scenario
    return {
        "scenario_slug": scenario.slug,
        "title": scenario.title,
        "description": scenario.description,
        "visibility": services.visibility_for(scenario),
        "latest_version": summary.latest_version,
        "published_versions": summary.published_versions,
        "created_at": scenario.created_at,
    }


def _serialize_version_summary(version: Any) -> dict[str, Any]:
    return {
        "manifest_version": version.version,
        "status": version.status,
        "published_at": version.published_at,
    }


class ScenarioCollectionView(APIView):
    """GET/POST /scenarios (api-spec §4.6 #26, #31)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_list",
        responses={200: serializers.ScenarioSummarySerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        requested = _query_workspace_id(request)
        workspace_id = _caller_workspace_id(request, requested)
        visibility = request.query_params.get("visibility")
        summaries = services.list_scenarios(workspace_id=workspace_id, visibility=visibility)
        data = [
            serializers.ScenarioSummarySerializer(_serialize_summary(s)).data for s in summaries
        ]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="scenarios_create_draft",
        request=serializers.DraftCreateSerializer,
        responses={201: serializers.ManifestVersionDetailSerializer},
    )
    def post(self, request: Request) -> Response:
        # Draft create is JWT-only (the AI/console seam): a key here is the wrong
        # credential → 401 handled by IsAuthenticated against the JWT principal.
        if isinstance(request.user, ApiKeyPrincipal):
            from config.problems import AuthenticationRequired

            raise AuthenticationRequired()
        serializer = serializers.DraftCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        workspace_id = cast(uuid.UUID, data["workspace_id"])
        _require_member(request, workspace_id, verified=True)
        arm_request_workspace(request._request, workspace_id)
        return self._create_draft(data["document"], workspace_id)

    def _create_draft(self, document: Any, workspace_id: uuid.UUID) -> Response:
        _check_document_size(document)
        try:
            ingest.enforce_draft_quota(workspace_id)
        except ingest.DraftQuotaExceeded as exc:
            raise RateLimited(str(exc)) from exc
        try:
            draft = ingest.create_draft(
                document, workspace_id=workspace_id, is_workspace_visibility=True
            )
        except ingest.ManifestParseError as exc:
            raise ManifestValidationFailed(
                "Manifest parsing failed.",
                errors=[exc.error.to_dict()],
            ) from exc
        except ingest.ManifestRejected as exc:
            raise _validation_failed(exc.report) from exc
        except ingest.SlugCollision as exc:
            raise ConflictError(str(exc)) from exc
        except ingest.VersionConflict as exc:
            raise ConflictError(str(exc)) from exc
        body = serializers.ManifestVersionDetailSerializer(_serialize_version_detail(draft)).data
        response = Response(body, status=status.HTTP_201_CREATED)
        response["Location"] = (
            f"/api/v1/scenarios/{draft.scenario.slug}/versions/{draft.version}"
        )
        return response


def _serialize_version_detail(version: Any) -> dict[str, Any]:
    return {
        "scenario_slug": version.scenario.slug,
        "manifest_version": version.version,
        "status": version.status,
        "sha256": version.manifest_sha256,
        "published_at": version.published_at,
        "document": version.manifest,
    }


def _check_document_size(document: Any) -> None:
    import json

    try:
        size = len(json.dumps(document).encode("utf-8"))
    except (TypeError, ValueError):
        return
    if size > _MAX_DOCUMENT_BYTES:
        raise PayloadTooLarge(
            "The manifest document exceeds the 512 KiB limit (B-01).",
            limit_bytes=_MAX_DOCUMENT_BYTES,
        )


def _validation_failed(report: dict[str, Any]) -> ManifestValidationFailed:
    errors = report.get("errors", [])
    return ManifestValidationFailed(
        f"{len(errors)} error(s) in semantic validation.",
        errors=errors,
        warnings=report.get("warnings", []),
    )


def _require_member(request: Request, workspace_id: uuid.UUID, *, verified: bool = False) -> str:
    """Resolve the JWT caller's role in ``workspace_id`` (foreign → 404)."""
    from tenancy.application import services as tenancy_services

    membership = tenancy_services.get_membership(workspace_id, _user(request))
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()
    if verified and not getattr(request.user, "is_verified", False):
        from config.problems import EmailNotVerified

        raise EmailNotVerified()
    role: str = membership.role
    return role


class ScenarioDetailView(APIView):
    """GET /scenarios/{scenario_slug} (api-spec §4.6 #27)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_retrieve",
        responses={200: serializers.ScenarioDetailSerializer},
    )
    def get(self, request: Request, scenario_slug: str) -> Response:
        workspace_id = _caller_workspace_id(request, _query_workspace_id(request))
        summary = services.get_scenario(scenario_slug, workspace_id=workspace_id)
        if summary is None:
            raise NotFoundError()
        body = _serialize_summary(summary)
        body["versions"] = [_serialize_version_summary(v) for v in summary.versions]
        return Response(serializers.ScenarioDetailSerializer(body).data)


class ScenarioVersionsView(APIView):
    """GET /scenarios/{scenario_slug}/versions (api-spec §4.6 #28)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_versions_list",
        responses={200: serializers.VersionSummarySerializer(many=True)},
    )
    def get(self, request: Request, scenario_slug: str) -> Response:
        workspace_id = _caller_workspace_id(request, _query_workspace_id(request))
        summary = services.get_scenario(scenario_slug, workspace_id=workspace_id)
        if summary is None:
            raise NotFoundError()
        data = [_serialize_version_summary(v) for v in summary.versions]
        return Response({"data": data, "next_cursor": None})


class ScenarioVersionDetailView(APIView):
    """GET /scenarios/{scenario_slug}/versions/{manifest_version} (api-spec §4.6 #29)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_version_retrieve",
        responses={200: serializers.ManifestVersionDetailSerializer},
    )
    def get(self, request: Request, scenario_slug: str, manifest_version: str) -> Response:
        workspace_id = _caller_workspace_id(request, _query_workspace_id(request))
        version = services.get_manifest_version(
            scenario_slug, manifest_version, workspace_id=workspace_id
        )
        if version is None:
            raise NotFoundError()
        body = serializers.ManifestVersionDetailSerializer(
            _serialize_version_detail(version)
        ).data
        return Response(body)


class ScenarioVersionValidationView(APIView):
    """GET …/versions/{manifest_version}/validation (api-spec §4.6 #30).

    JWT-only (the L3 dry-run polling target). Returns the persisted §8.3
    ValidationReport; L3 dry-run lands in Phase 4, so the report's ``dry_run``
    member is ``null`` for L1+L2-only versions.
    """

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_version_validation",
        responses={200: serializers.ValidationReportSerializer},
    )
    def get(self, request: Request, scenario_slug: str, manifest_version: str) -> Response:
        workspace_id = _caller_workspace_id(request, _query_workspace_id(request))
        version = services.get_manifest_version(
            scenario_slug, manifest_version, workspace_id=workspace_id
        )
        if version is None:
            raise NotFoundError()
        report = version.validation_report or {"status": "pending", "errors": [], "warnings": []}
        return Response(serializers.ValidationReportSerializer(report).data)


class ScenarioPublishView(APIView):
    """POST …/versions/{manifest_version}/publish (api-spec §4.6 #32).

    JWT, admin of the owning workspace. Requires the persisted ValidationReport
    passed (INV-CAT-2); derives + registers v1 schemas transactionally (R-DER).
    Unpassed validation → 409 ``conflict``; a non-additive derived schema (only on
    a re-publish minor version) → 422 with MAN-V501 errors.
    """

    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenarios_version_publish",
        request=None,
        responses={200: serializers.ManifestVersionDetailSerializer},
    )
    def post(self, request: Request, scenario_slug: str, manifest_version: str) -> Response:
        owner_ws = self._authorize_owner(request, scenario_slug)
        version = services.get_manifest_version(
            scenario_slug, manifest_version, workspace_id=owner_ws
        )
        if version is None:
            raise NotFoundError()
        return self._publish(version, owner_ws, actor=request.user)

    def _authorize_owner(self, request: Request, scenario_slug: str) -> uuid.UUID:
        """Admin-of-owning-workspace check (api-spec #32).

        The slug is workspace-unique (§4.1): search the caller's memberships for the
        workspace that owns a ``workspace``-visibility scenario by this slug. A
        global (builtin) scenario has no owning workspace and is published only by
        the maintenance loader (``sync_builtin_scenarios``), never this JWT route —
        it masks to 404 here. Requires the owning workspace's admin role.
        """
        from tenancy.application import services as tenancy_services

        owning_role: tuple[uuid.UUID, str] | None = None
        for summary_row in tenancy_services.membership_summaries(_user(request)):
            ws_id = uuid.UUID(str(summary_row["workspace_id"]))
            scn = services.get_scenario(scenario_slug, workspace_id=ws_id)
            if scn is not None and scn.scenario.workspace_id == ws_id:
                owning_role = (ws_id, str(summary_row["role"]))
                break
        if owning_role is None:
            raise NotFoundError()  # no owning workspace the caller belongs to
        workspace_id, role = owning_role
        if role != ROLE_ADMIN:
            raise PermissionDeniedError(
                "Publishing a manifest version requires the workspace admin role.",
                required_role=ROLE_ADMIN,
            )
        arm_request_workspace(request._request, workspace_id)
        return workspace_id

    def _publish(self, version: Any, owner_ws: uuid.UUID | None, *, actor: Any) -> Response:
        try:
            result = publish.publish_manifest_version(
                version, actor=actor, workspace_id=owner_ws
            )
        except publish.PublishNotReady as exc:
            raise ConflictError(str(exc)) from exc
        except publish.AlreadyPublished as exc:
            raise ConflictError(str(exc)) from exc
        except publish.ManifestSchemaCompatError as exc:
            raise ManifestValidationFailed(str(exc), errors=exc.errors) from exc
        body = serializers.ManifestVersionDetailSerializer(
            _serialize_version_detail(result.manifest_version)
        ).data
        return Response(body)


# --- scenario instances (api-spec §4.7 #33-38) ------------------------------


def _serialize_instance(instance: Any) -> dict[str, Any]:
    return {
        "scenario_instance_id": instance.id,
        "workspace_id": instance.workspace_id,
        "name": instance.name,
        "scenario_slug": instance.scenario.slug,
        "manifest_version": instance.scenario_definition.version,
        "config_revision": instance.config_version,
        "created_at": instance.created_at,
        "updated_at": instance.updated_at,
    }


def _resolve_ws_for_read(request: Request, workspace_id: str) -> Any:
    """Resolve + arm a workspace for a JWT-or-Key read (api-spec A-5, #33/#35/#36).

    Key: the path workspace MUST equal the key's workspace (W-1) else 404, and the
    key needs ``streams:read``. JWT: membership in the path workspace else 404.
    Arms the workspace context on success so the Class-T scoped manager filters.
    """
    ws_uuid = _uuid(workspace_id)
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if principal.workspace_id != ws_uuid:
            raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
        if "streams:read" not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope="streams:read"
            )
        arm_request_workspace(request._request, ws_uuid)
        return _live_workspace(ws_uuid)
    _require_member(request, ws_uuid)
    arm_request_workspace(request._request, ws_uuid)
    return _live_workspace(ws_uuid)


def _resolve_ws_member(request: Request, workspace_id: str) -> Any:
    """Resolve + arm a workspace for a JWT member write (foreign → 404)."""
    ws_uuid = _uuid(workspace_id)
    _require_member(request, ws_uuid)
    arm_request_workspace(request._request, ws_uuid)
    return _live_workspace(ws_uuid)


def _live_workspace(workspace_id: uuid.UUID) -> Any:
    from tenancy.domain.models import Workspace

    # Workspace is self-tenant-owned (Class W): its default manager is plain (no
    # workspace_id column to scope on), so ``objects`` is the right accessor.
    workspace = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
    if workspace is None:
        raise NotFoundError()
    return workspace


class ScenarioInstanceCollectionView(APIView):
    """GET/POST …/scenario-instances (api-spec §4.7 #33-34)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenario_instances_list",
        responses={200: serializers.ScenarioInstanceSerializer(many=True)},
    )
    def get(self, request: Request, workspace_id: str) -> Response:
        workspace = _resolve_ws_for_read(request, workspace_id)
        rows = services.list_instances(workspace=workspace)
        data = [serializers.ScenarioInstanceSerializer(_serialize_instance(r)).data for r in rows]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="scenario_instances_create",
        request=serializers.InstanceCreateSerializer,
        responses={201: serializers.ScenarioInstanceSerializer},
    )
    def post(self, request: Request, workspace_id: str) -> Response:
        if isinstance(request.user, ApiKeyPrincipal):
            from config.problems import AuthenticationRequired

            raise AuthenticationRequired()  # instance create is JWT member-only
        workspace = _resolve_ws_member(request, workspace_id)
        serializer = serializers.InstanceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        try:
            instance = services.create_instance(
                workspace=workspace,
                name=data["name"],
                scenario_slug=data["scenario_slug"],
                manifest_version=data["manifest_version"],
                configuration=data.get("configuration") or {},
                default_seed=data.get("default_seed"),
                actor=request.user,
            )
        except services.InstancePinDeprecated as exc:
            raise ConflictError(str(exc)) from exc
        except services.InstanceNameConflict as exc:
            raise ConflictError(str(exc)) from exc
        except services.InstanceOverlayRejected as exc:
            raise _validation_failed(exc.report) from exc
        body = serializers.ScenarioInstanceSerializer(_serialize_instance(instance)).data
        response = Response(body, status=status.HTTP_201_CREATED)
        response["Location"] = (
            f"/api/v1/workspaces/{workspace.id}/scenario-instances/{instance.id}"
        )
        return response


class ScenarioInstanceDetailView(APIView):
    """GET/DELETE …/scenario-instances/{id} (api-spec §4.7 #35, #38)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenario_instances_retrieve",
        responses={200: serializers.ScenarioInstanceSerializer},
    )
    def get(self, request: Request, workspace_id: str, scenario_instance_id: str) -> Response:
        workspace = _resolve_ws_for_read(request, workspace_id)
        instance = services.get_instance(_uuid(scenario_instance_id), workspace=workspace)
        if instance is None:
            raise NotFoundError()
        return Response(serializers.ScenarioInstanceSerializer(_serialize_instance(instance)).data)

    @extend_schema(operation_id="scenario_instances_delete", responses={204: None})
    def delete(self, request: Request, workspace_id: str, scenario_instance_id: str) -> Response:
        if isinstance(request.user, ApiKeyPrincipal):
            from config.problems import AuthenticationRequired

            raise AuthenticationRequired()
        workspace = _resolve_ws_member(request, workspace_id)
        instance = services.get_instance(_uuid(scenario_instance_id), workspace=workspace)
        if instance is None:
            raise NotFoundError()
        try:
            services.delete_instance(instance=instance, actor=request.user)
        except services.InstanceHasStreams as exc:
            raise ConflictError(str(exc)) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)


class ScenarioInstanceConfigurationView(APIView):
    """GET/PUT …/scenario-instances/{id}/configuration (api-spec §4.7 #36-37)."""

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="scenario_instances_configuration_retrieve",
        responses={200: serializers.ConfigurationSerializer},
    )
    def get(self, request: Request, workspace_id: str, scenario_instance_id: str) -> Response:
        workspace = _resolve_ws_for_read(request, workspace_id)
        instance = services.get_instance(_uuid(scenario_instance_id), workspace=workspace)
        if instance is None:
            raise NotFoundError()
        body = {"config_revision": instance.config_version, "configuration": instance.overrides}
        return Response(serializers.ConfigurationSerializer(body).data)

    @extend_schema(
        operation_id="scenario_instances_configuration_replace",
        request=serializers.ConfigurationReplaceSerializer,
        responses={200: serializers.ConfigurationSerializer},
    )
    def put(self, request: Request, workspace_id: str, scenario_instance_id: str) -> Response:
        if isinstance(request.user, ApiKeyPrincipal):
            from config.problems import AuthenticationRequired

            raise AuthenticationRequired()
        workspace = _resolve_ws_member(request, workspace_id)
        instance = services.get_instance(_uuid(scenario_instance_id), workspace=workspace)
        if instance is None:
            raise NotFoundError()
        serializer = serializers.ConfigurationReplaceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        configuration = dict(serializer.validated_data)["configuration"]
        try:
            instance = services.replace_configuration(
                instance=instance, configuration=configuration, actor=request.user
            )
        except services.InstanceOverlayRejected as exc:
            raise _validation_failed(exc.report) from exc
        body = {"config_revision": instance.config_version, "configuration": instance.overrides}
        return Response(serializers.ConfigurationSerializer(body).data)


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc
