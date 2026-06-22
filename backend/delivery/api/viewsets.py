"""DRF viewsets for the Delivery context (backend-architecture §3.1, api layer).

The REST cursor pull (delivery-channels §5): ``GET /api/v1/streams/{id}/events`` —
at-least-once, replayable, client-paced reads over ``event_buffer``. Authenticated by
an API key with ``events:read`` (``X-API-Key``) or a console JWT for workspace
members (auth matrix A-5). The owning workspace masks foreign access to **404**
(never 403, which would confirm existence — security §3.3 / RC-5); an authenticated
key/JWT in its own workspace lacking ``events:read`` gets **403** with
``required_scope``. Per-key rate limiting (§5.1) caps the poll rate.

Cursor + page semantics live in :mod:`delivery.application.services`; this view does
auth, masking, rate limiting, request shaping, and maps the two cursor domain errors
to their RFC 9457 problem types (``400 cursor-invalid`` / ``410 cursor-expired``).
"""

from __future__ import annotations

import uuid
from typing import cast

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import (
    CursorExpired,
    CursorInvalid,
    NotFoundError,
    PermissionDeniedError,
    RateLimited,
)
from delivery.api import serializers
from delivery.application import services
from identity.infra.jwt import DataForgeJWTAuthentication
from tenancy.api.authentication import ApiKeyAuthentication, ApiKeyPrincipal
from tenancy.api.middleware import arm_request_workspace

# The data-plane read scope this surface requires (security §3.2.2 / A-4).
_SCOPE_READ = "events:read"

# Per-key rate limit for the cursor pull (§5.1 — per-key hard bound). The fixed
# window is generous (steady-state tail polling is ≥ 1 s apart by guidance, RC-2).
_RATE_LIMIT_PER_MINUTE = 600


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise NotFoundError() from exc  # malformed id masks to 404 (W-3)


class StreamEventsView(APIView):
    """GET /streams/{stream_id}/events — the REST cursor pull (api-spec §4.9.1; §5).

    **Ordering semantics (sharding, scaling-strategy §2.2/§3).** Events are ordered
    **per ``partition_key``** — all events for one partition entity (an actor and its
    CDC, PK-1..3, event-model §2.2.3) are delivered in a stable, replay-consistent
    order. A multi-shard stream (``shard_count > 1``) partitions actors to disjoint
    shards by a stable hash of their primary key, so each actor's events always come
    from one shard; **across shards the interleaving is unordered** — there is no
    global total order over a stream, only per-``partition_key`` order. The cursor is
    opaque and replay-stable within that contract (RC-7); ``shard_count`` is pinned at
    stream start and never changes, so an actor's partition assignment is immutable.
    """

    authentication_classes: list[type] = [ApiKeyAuthentication, DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="streams_events",
        description=(
            "Pull a page of delivered events by opaque cursor. **Ordering is per "
            "`partition_key`**: all events for one partition entity (an actor and its "
            "CDC) are delivered in a stable, replay-consistent order. For a multi-shard "
            "stream actors are partitioned to disjoint shards by a stable hash of their "
            "key, so one actor's events always come from one shard; **cross-shard "
            "interleaving is unordered** — there is no global total order over a stream. "
            "`shard_count` is pinned at stream start and immutable."
        ),
        parameters=[
            OpenApiParameter("cursor", str, description="Opaque resume cursor (RC-7)."),
            OpenApiParameter(
                "from", str, description="earliest | latest | RFC-3339 start position."
            ),
            OpenApiParameter("limit", int, description="Page size 1..1000 (default 100)."),
            OpenApiParameter("types", str, description="Comma list, ≤20 event-type filter."),
            OpenApiParameter(
                "entity_type",
                str,
                description="Per-entity CDC filter (R-CDC-7); pair with entity_key.",
            ),
            OpenApiParameter(
                "entity_key",
                str,
                description="Per-entity CDC filter (R-CDC-7); pair with entity_type.",
            ),
        ],
        responses={200: serializers.EventsPageSerializer},
    )
    def get(self, request: Request, stream_id: str) -> Response:
        stream_uuid = _uuid(stream_id)
        # Resolve the stream's workspace (foreign → 404 BEFORE any state is revealed),
        # require events:read, arm the scoped context for the buffer read.
        workspace_id = self._authorize(request, stream_uuid)
        self._rate_limit(request, workspace_id)

        params = serializers.EventsQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        validated = params.validated_data

        query = services.EventsQuery(
            stream_id=str(stream_uuid),
            limit=int(validated.get("limit", 100)),
            cursor=validated.get("cursor"),
            from_spec=validated.get("from"),
            types=tuple(validated.get("types", ())),
            entity_type=validated.get("entity_type"),
            entity_key=validated.get("entity_key"),
        )
        from observation.infra import metrics

        try:
            page = services.read_events(query)
        except services.CursorInvalidError as exc:
            raise CursorInvalid(str(exc)) from exc
        except services.CursorExpiredError as exc:
            # df_cursor_expired_total: a 410 cursor-expired (retention window passed,
            # observability §4 web family / CursorExpiredSpike ticket alert source).
            metrics.cursor_expired_total.inc()
            raise CursorExpired(
                earliest_cursor=exc.earliest_cursor,
                retention_hours=exc.retention_hours,
            ) from exc

        rows = list(page.data)
        # df_events_served_total{channel=rest}: the bulk cursor-pull path (limit caps
        # at 1000, §5.1; SLO-2 / delivery-throughput source). Counts events actually
        # returned to the consumer this page.
        if rows:
            metrics.events_served_total.labels(channel="rest").inc(len(rows))
        return Response({"data": rows, "next_cursor": page.next_cursor})

    # -- auth + masking (security §3.3; RC-5) -----------------------------------

    def _authorize(self, request: Request, stream_id: uuid.UUID) -> uuid.UUID:
        """Resolve the stream's workspace, mask foreign → 404, require events:read.

        Reads the stream by unique id (unscoped) to derive its workspace, then:
        a key must be pinned to that workspace (foreign → 404, W-1) and carry
        ``events:read`` (else 403 within own workspace); a JWT caller must be a
        member (foreign → 404). The scoped context is armed so the buffer read is
        RLS-filtered and re-confirms the stream is visible (else 404).
        """
        from streams.domain.models import Stream
        from tenancy.application.services import platform_read_scope

        # tenancy: unscoped — single-resource route resolves the owning workspace
        # from the unique id, then re-checks access + arms the scoped context (W-2).
        # The pre-arm read runs under platform_read_scope so the strict Class T RLS
        # policy admits the row to the NOBYPASSRLS runtime role before any workspace
        # is armed (backend-architecture §4.2; read-only — foreign access is still
        # masked to 404 by the key/membership check + the scoped re-read below).
        with platform_read_scope():
            row = Stream.all_objects.filter(id=stream_id).first()
        if row is None:
            raise NotFoundError()
        workspace_id = uuid.UUID(str(row.workspace_id))

        principal = request.user
        if isinstance(principal, ApiKeyPrincipal):
            if principal.workspace_id != workspace_id:
                raise NotFoundError()  # foreign workspace for the key → 404 (W-1)
            if _SCOPE_READ not in principal.scopes:
                raise PermissionDeniedError(
                    "The API key lacks a required scope.", required_scope=_SCOPE_READ
                )
        else:
            self._require_member(request, workspace_id)

        arm_request_workspace(request._request, workspace_id)
        # Re-read through the scoped manager so a foreign/absent stream masks to 404
        # even after arming (defense in depth; the unscoped row above only derived
        # the workspace).
        if not Stream.objects.filter(id=stream_id).exists():
            raise NotFoundError()
        return workspace_id

    def _require_member(self, request: Request, workspace_id: uuid.UUID) -> None:
        """The JWT caller must be a member of ``workspace_id`` (foreign → 404, W-3)."""
        from identity.domain.models import User
        from tenancy.application import services as tenancy_services

        user = cast("User", request.user)
        membership = tenancy_services.get_membership(workspace_id, user)
        if membership is None or membership.workspace.deleted_at is not None:
            raise NotFoundError()

    # -- rate limiting (§5.1) ---------------------------------------------------

    def _rate_limit(self, request: Request, workspace_id: uuid.UUID) -> None:
        """Per-key (or per-workspace JWT) fixed-window cap on the poll rate (§5.1).

        Keyed on the API key id (the per-key hard bound, RC-2) or the workspace +
        user for JWT callers. Fails open if Redis is degraded (the limiter is an
        abuse control, not an auth primitive — identity §5.4).
        """
        from identity.infra.rate_limit import Window, check

        principal = request.user
        if isinstance(principal, ApiKeyPrincipal):
            identifier = f"key:{principal.api_key_id}"
        else:
            identifier = f"jwt:{workspace_id}:{getattr(principal, 'id', 'anon')}"
        result = check(
            "events_pull", identifier, (Window(limit=_RATE_LIMIT_PER_MINUTE, seconds=60),)
        )
        if not result.allowed:
            raise RateLimited(retry_after=result.retry_after)
