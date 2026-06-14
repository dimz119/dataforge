"""First-message WebSocket auth + stream-ownership resolution (delivery-channels §6.2;
backend-architecture §10; api-spec §5).

The synchronous (DB-touching) core of the WS handshake's ``auth`` frame, called by the
async consumer through ``channels.db.database_sync_to_async``:

* resolve the credential — ``api_key`` (scope ``events:read``, the canonical machine
  path) or ``access_token`` (console JWT of a workspace member, the monitoring path)
  — exactly the auth matrix A-5 (WS-2). Query-string credentials never reach here:
  the consumer reads creds only from the auth frame body (WS-2);
* resolve the URL stream's owning workspace by unique id under ``platform_read_scope``
  (the same pre-context read the REST viewset does, backend-architecture §4.2), mask a
  foreign/absent stream to ``4404`` (anti-enumeration, mirrors RC-5/W-3, WS-3);
* enforce the scope (``4403``) / membership (``4404``) gates, then arm nothing — the
  consumer does not write, so no RLS context is needed for the live tail (the group
  fan-out is a Redis op, INV-DEL-6 rides on the auth gate + unique stream id).

The outcome is a :class:`WsAuthResult` carrying either a ``close_code`` (the §6.5
failure code the consumer closes with) or the resolved ``workspace_id`` + the
credential's ``key_prefix`` (for the revoked-key live-disconnect watch, WS-3).

This is application-layer code: it imports tenancy/identity *application + domain*
(allowed by the cross-app contract) and the config problem types, never another app's
``api``/``infra``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from delivery.domain.ws_protocol import (
    CLOSE_AUTH_FAILED,
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    SCOPE_EVENTS_READ,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["WsAuthResult", "resolve_ws_auth"]


@dataclass(frozen=True)
class WsAuthResult:
    """The outcome of resolving an ``auth`` frame (§6.2).

    On success ``close_code`` is ``None`` and ``workspace_id`` is the resolved owning
    workspace; ``key_prefix`` is the API key's prefix (for the < 1 s revoked-key
    live-disconnect watch, WS-3) or ``None`` for a JWT caller. On failure
    ``close_code`` is the §6.5 code to close with and the rest are ``None``.
    """

    close_code: int | None
    workspace_id: uuid.UUID | None = None
    key_prefix: str | None = None


def _fail(code: int) -> WsAuthResult:
    return WsAuthResult(close_code=code)


def resolve_ws_auth(*, stream_id: str, frame: Mapping[str, object]) -> WsAuthResult:
    """Resolve one ``auth`` frame to a :class:`WsAuthResult` (§6.2 / WS-2/WS-3).

    ``stream_id`` is the URL-named stream (one stream per connection). ``frame`` is the
    parsed ``auth`` frame; exactly one of ``api_key`` / ``access_token`` must be
    present. Runs synchronously — the consumer wraps it in ``database_sync_to_async``.

    The whole resolution runs inside one ``transaction.atomic()`` block. Unlike the
    REST request path (``ATOMIC_REQUESTS``), the Channels consumer's
    ``database_sync_to_async`` call is NOT wrapped in a request transaction, so the
    autocommit default would make the ``SET LOCAL`` GUCs the auth-bootstrap lookups
    rely on (``app.api_key_prefix`` Class K, ``app.platform`` pre-context read) die
    between the SET and the SELECT — silently denying the row and masking a valid
    cold-cache (or foreign) key as 4401 instead of resolving it (→ 4404 for foreign,
    security §3.2 / §4.2). One transaction keeps the SET LOCAL alive across the
    bootstrap query, exactly mirroring the REST request transaction.
    """
    from django.db import transaction

    try:
        stream_uuid = uuid.UUID(str(stream_id))
    except (ValueError, AttributeError, TypeError):
        return _fail(CLOSE_NOT_FOUND)  # malformed id masks to 4404 (anti-enum)

    api_key = frame.get("api_key")
    access_token = frame.get("access_token")
    if (api_key is None) == (access_token is None):
        # Neither, or both → no single principal: authentication failed (4401).
        return _fail(CLOSE_AUTH_FAILED)

    with transaction.atomic():
        workspace_id = _stream_workspace(stream_uuid)
        if workspace_id is None:
            return _fail(CLOSE_NOT_FOUND)  # absent stream masks to 4404 (WS-3)

        if api_key is not None:
            return _resolve_api_key(
                presented=str(api_key), stream_workspace=workspace_id
            )
        return _resolve_access_token(
            token=str(access_token), stream_workspace=workspace_id
        )


def _stream_workspace(stream_id: uuid.UUID) -> uuid.UUID | None:
    """The owning workspace of ``stream_id`` (None if absent), under platform read.

    The pre-context read runs under ``platform_read_scope`` so the strict Class T RLS
    policy admits the row to the NOBYPASSRLS runtime role before any workspace is armed
    (backend-architecture §4.2); foreign access is still masked to 4404 by the
    credential checks below.
    """
    from streams.domain.models import Stream
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        row = Stream.all_objects.filter(id=stream_id).first()  # tenancy: unscoped pre-context
    if row is None:
        return None
    return uuid.UUID(str(row.workspace_id))


def _resolve_api_key(*, presented: str, stream_workspace: uuid.UUID) -> WsAuthResult:
    """API-key path: verify → workspace-pin (4404) → scope (4403) (WS-2/WS-3).

    A foreign-workspace key is masked to ``4404`` (anti-enumeration, W-1), never
    ``4403`` — the key holder must not learn the stream exists. A correctly-scoped key
    in its own workspace lacking ``events:read`` is ``4403``.
    """
    from config.problems import InvalidApiKey
    from tenancy.application import keys as key_service

    try:
        verified = key_service.verify_key(presented)
    except InvalidApiKey:
        return _fail(CLOSE_AUTH_FAILED)  # invalid/revoked/expired → 4401

    if verified.workspace_id != stream_workspace:
        return _fail(CLOSE_NOT_FOUND)  # foreign workspace → 4404 (W-1)
    if SCOPE_EVENTS_READ not in verified.scopes:
        return _fail(CLOSE_FORBIDDEN)  # own workspace, missing scope → 4403

    prefix = _key_prefix(verified.api_key_id)
    return WsAuthResult(
        close_code=None, workspace_id=stream_workspace, key_prefix=prefix
    )


def _key_prefix(api_key_id: uuid.UUID) -> str | None:
    """The key's ``key_prefix`` for the revoked-key watch (WS-3). None if unreadable."""
    from tenancy.domain.models import ApiKey
    from tenancy.infra import guc

    # Read the key's own row by id under its workspace context (Class K by-id read).
    api_key = ApiKey.all_objects.filter(id=api_key_id).first()  # tenancy: own-key by id
    if api_key is not None:
        return str(api_key.key_prefix)
    # Cache-only path (DB row not visible here): fall back to no watch (the 60 s
    # active-TTL still bounds staleness, SEC-KEY-6).
    _ = guc  # imported for parity with the keys service; no GUC needed by-id here
    return None


def _resolve_access_token(*, token: str, stream_workspace: uuid.UUID) -> WsAuthResult:
    """JWT path: validate token → live user → workspace membership (4404) (WS-2/WS-3).

    A non-member (or a member of a foreign/deleted workspace) is masked to ``4404``
    (anti-enumeration, mirrors the REST viewset's ``_require_member``). A JWT has no
    scope dimension on this surface — membership is the gate (A-5).
    """
    from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

    from identity.infra.jwt import DataForgeJWTAuthentication

    auth = DataForgeJWTAuthentication()
    try:
        validated = auth.get_validated_token(token.encode("utf-8"))
        user = auth.get_user(validated)
    except (InvalidToken, TokenError, Exception):
        return _fail(CLOSE_AUTH_FAILED)

    from tenancy.application import services as tenancy_services

    membership = tenancy_services.get_membership(stream_workspace, user)  # type: ignore[arg-type]
    if membership is None or membership.workspace.deleted_at is not None:
        return _fail(CLOSE_NOT_FOUND)  # non-member / deleted → 4404 (W-3)

    return WsAuthResult(close_code=None, workspace_id=stream_workspace, key_prefix=None)
