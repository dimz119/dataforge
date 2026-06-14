"""Generation API views — the datasets (backfill batch) surface (api-spec §4.10,
routes #57-61).

Five endpoints with the dual JWT|API-key auth shape (api-spec A-5):

* ``POST /datasets`` (#57) — JWT | Key(``streams:write``). The owning workspace is
  the request-body ``workspace_id`` (JWT: must be a member; key: must equal the
  key's workspace, else 404 W-1). ``201`` sync (estimate ≤ threshold) or ``202``
  async with a Celery job; both carry a ``Location`` header.
* ``GET /datasets`` (#58) — JWT | Key(``streams:read``); the caller's datasets,
  filterable by ``status``.
* ``GET /datasets/{id}`` (#59) — JWT | Key(``streams:read``); foreign id → 404.
* ``GET /datasets/{id}/download`` (#60) — Key(``events:read``) | JWT; streams the
  gzipped JSONL (delivered-shape, ``_df`` stripped). Non-``ready`` → 409.
* ``DELETE /datasets/{id}`` (#61) — JWT | Key(``streams:write``); removes file +
  row, ``204``.

The dataset id is the discriminator (OBJECT class): the scoped manager filters by
the armed workspace, so a foreign id resolves to no row → 404 (W-3 masking).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.http import FileResponse
from django.http.response import HttpResponseBase
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import (
    InvalidStateTransition,
    NotFoundError,
    PermissionDeniedError,
    QuotaExceeded,
)
from generation.api import serializers
from generation.application import services
from generation.domain.models import DATASET_READY, Dataset
from identity.infra.jwt import DataForgeJWTAuthentication
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace

_AUTH: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc


def _body_workspace_id(request: Request) -> uuid.UUID:
    """The body ``workspace_id`` (POST). Absent/malformed → 404 (W-3 masking)."""
    raw = request.data.get("workspace_id") if isinstance(request.data, dict) else None
    if not raw:
        raise NotFoundError()
    return _uuid(str(raw))


def _query_workspace_id(request: Request) -> uuid.UUID:
    """The query ``workspace_id`` (GET/DELETE/download). Absent → 404 (W-3 masking).

    Every dataset route names its owning workspace explicitly (no implicit "the
    key's workspace") so a foreign credential without a workspace masks to 404
    uniformly across JWT and key (the SCOPE cross-tenant contract).
    """
    raw = request.query_params.get("workspace_id")
    if not raw:
        raise NotFoundError()
    return _uuid(raw)


def _live_workspace(workspace_id: uuid.UUID) -> Any:
    from tenancy.domain.models import Workspace

    workspace = Workspace.objects.filter(id=workspace_id, deleted_at__isnull=True).first()
    if workspace is None:
        raise NotFoundError()
    return workspace


def _resolve_ws(request: Request, workspace_id: uuid.UUID, *, scope: str) -> Any:
    """Resolve + arm the owning workspace for a JWT-or-key request (A-5, W-1).

    Key: the workspace MUST equal the key's workspace (else 404), and the key needs
    ``scope``. JWT: membership in the workspace (else 404). Arms the workspace
    context so the Class-T scoped manager + RLS filter to it.
    """
    principal = request.user
    if isinstance(principal, ApiKeyPrincipal):
        if principal.workspace_id != workspace_id:
            raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
        if scope not in principal.scopes:
            raise PermissionDeniedError(
                "The API key lacks a required scope.", required_scope=scope
            )
        arm_request_workspace(request._request, workspace_id)
        return _live_workspace(workspace_id)
    _require_member(request, workspace_id)
    arm_request_workspace(request._request, workspace_id)
    return _live_workspace(workspace_id)


def _require_member(request: Request, workspace_id: uuid.UUID) -> None:
    from tenancy.application import services as tenancy_services

    user: Any = request.user
    membership = tenancy_services.get_membership(workspace_id, user)
    if membership is None or membership.workspace.deleted_at is not None:
        raise NotFoundError()


def _serialize(dataset: Dataset) -> dict[str, Any]:
    return {
        "dataset_id": dataset.id,
        "workspace_id": dataset.workspace_id,
        "scenario_instance_id": dataset.scenario_instance_id,
        "name": dataset.name,
        "status": dataset.status,
        "progress": dataset.progress,
        "seed": str(dataset.seed),
        "pin_sha256": dataset.pin_sha256,
        "simulated_window": {
            "from": dataset.simulated_from,
            "to": dataset.simulated_to,
        },
        "estimated_events": dataset.estimated_events,
        "event_count": dataset.event_count,
        "size_bytes": dataset.size_bytes,
        "compression": dataset.compression,
        "created_at": dataset.created_at,
        "ready_at": dataset.ready_at,
        "expires_at": dataset.expires_at,
        "failure_reason": dataset.failure_reason,
        "download_path": f"/api/v1/datasets/{dataset.id}/download",
    }


class DatasetCollectionView(APIView):
    """GET/POST /datasets (api-spec §4.10 #57-58)."""

    authentication_classes: list[type] = _AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="datasets_list",
        responses={200: serializers.DatasetSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        workspace = self._resolve_for_read(request)
        rows = services.list_datasets(
            workspace=workspace, status=request.query_params.get("status")
        )
        data = [serializers.DatasetSerializer(_serialize(r)).data for r in rows]
        return Response({"data": data, "next_cursor": None})

    @extend_schema(
        operation_id="datasets_create",
        request=serializers.DatasetCreateSerializer,
        responses={
            201: serializers.DatasetSerializer,
            202: serializers.DatasetSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        # Resolve + arm the owning workspace from the body BEFORE full validation so
        # a foreign/absent workspace masks to 404 (W-3) ahead of any body error — the
        # cross-tenant contract for this body-discriminated route (SCOPE class).
        workspace = _resolve_ws(request, _body_workspace_id(request), scope="streams:write")
        serializer = serializers.DatasetCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if data["workspace_id"] != workspace.id:
            raise NotFoundError()  # body workspace_id changed under us → mask

        try:
            result = services.create_dataset(
                workspace=workspace,
                scenario_instance_id=data["scenario_instance_id"],
                name=data["name"],
                seed=data.get("seed"),
                simulated_days=data["simulated_days"],
                virtual_epoch=data.get("virtual_epoch"),
                compression=data["compression"],
                actor=request.user,
            )
        except services.InstanceNotFound as exc:
            raise NotFoundError() from exc
        except services.QuotaExceeded as exc:
            raise QuotaExceeded(
                str(exc), quota=exc.quota, limit=exc.limit, requested=exc.requested
            ) from exc
        if not result.sync:
            # Large batch → hand off to the exports queue (api/tasks may import tasks).
            # ATOMIC_REQUESTS wraps this handler in one transaction (settings base
            # §101); enqueue only AFTER it commits so the worker (a separate
            # connection) can see the freshly-created Dataset row — enqueuing inline
            # races the commit and the task reads "dataset not found".
            from django.db import transaction

            from generation.tasks import enqueue_dataset_generation

            dataset_id = str(result.dataset.id)
            ws_id = str(workspace.id)
            transaction.on_commit(
                lambda: enqueue_dataset_generation(dataset_id, ws_id)
            )
        body = serializers.DatasetSerializer(_serialize(result.dataset)).data
        code = status.HTTP_201_CREATED if result.sync else status.HTTP_202_ACCEPTED
        response = Response(body, status=code)
        response["Location"] = f"/api/v1/datasets/{result.dataset.id}"
        return response

    def _resolve_for_read(self, request: Request) -> Any:
        return _resolve_ws(request, _query_workspace_id(request), scope="streams:read")


class DatasetDetailView(APIView):
    """GET/DELETE /datasets/{dataset_id} (api-spec §4.10 #59, #61)."""

    authentication_classes: list[type] = _AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="datasets_retrieve",
        responses={200: serializers.DatasetSerializer},
    )
    def get(self, request: Request, dataset_id: str) -> Response:
        dataset = self._resolve_dataset(request, dataset_id, scope="streams:read")
        return Response(serializers.DatasetSerializer(_serialize(dataset)).data)

    @extend_schema(operation_id="datasets_delete", responses={204: None})
    def delete(self, request: Request, dataset_id: str) -> Response:
        dataset = self._resolve_dataset(request, dataset_id, scope="streams:write")
        from generation.infra.storage import delete_artifact

        delete_artifact(dataset.file_path)
        dataset.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _resolve_dataset(self, request: Request, dataset_id: str, *, scope: str) -> Dataset:
        ds_id = _uuid(dataset_id)
        workspace = self._owning_workspace(request, scope=scope)
        dataset = services.get_dataset(ds_id, workspace=workspace)
        if dataset is None:
            raise NotFoundError()
        return dataset

    def _owning_workspace(self, request: Request, *, scope: str) -> Any:
        return _resolve_ws(request, _query_workspace_id(request), scope=scope)


class DatasetDownloadView(APIView):
    """GET /datasets/{dataset_id}/download (api-spec §4.10.3 #60)."""

    authentication_classes: list[type] = _AUTH
    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id="datasets_download", responses={200: None})
    def get(self, request: Request, dataset_id: str) -> HttpResponseBase:
        ds_id = _uuid(dataset_id)
        workspace = self._owning_workspace(request)
        dataset = services.get_dataset(ds_id, workspace=workspace)
        if dataset is None:
            raise NotFoundError()
        if dataset.status != DATASET_READY:
            raise InvalidStateTransition(
                "The dataset is not ready for download (api-spec §4.10.3)."
            )
        return self._stream(dataset)

    def _owning_workspace(self, request: Request) -> Any:
        return _resolve_ws(request, _query_workspace_id(request), scope="events:read")

    def _stream(self, dataset: Dataset) -> HttpResponseBase:
        from pathlib import Path

        path = Path(dataset.file_path)
        if not path.exists():
            raise NotFoundError()
        is_gzip = dataset.compression == "gzip"
        content_type = "application/gzip" if is_gzip else "application/x-ndjson"
        ext = ".jsonl.gz" if is_gzip else ".jsonl"
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(path.stat().st_size)
        response["Content-Disposition"] = (
            f'attachment; filename="{dataset.name}{ext}"'
        )
        return response
