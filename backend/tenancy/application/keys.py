"""API-key lifecycle + verification (security §3.2; ADR-0011; INV-TEN-4).

Create (reveal-once 201), list (prefix+last4 only), revoke (DB txn + synchronous
Redis write before 204, SEC-KEY-5), and the verification path used by the
``ApiKeyAuthentication`` DRF class: Redis revocation cache → constant-time hash
compare → derived-state check. Audit ``tenancy.api_key.created`` /
``tenancy.api_key.revoked`` in the same transaction as the mutation (INV-AUD-2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from config.problems import InvalidApiKey, PermissionDeniedError
from identity.application.permissions import require_verified
from identity.domain.models import User
from tenancy.application.audit import emit
from tenancy.domain.models import (
    ADMIN_ONLY_SCOPES,
    KEY_SCOPES,
    ROLE_ADMIN,
    ApiKey,
    Membership,
    Workspace,
)
from tenancy.infra import guc, revocation_cache
from tenancy.infra import keys as key_crypto


@dataclass(frozen=True)
class VerifiedKey:
    """The result of a successful API-key verification (request principal)."""

    api_key_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: list[str]


def create_key(
    *,
    workspace: Workspace,
    actor: User,
    name: str,
    scopes: list[str],
    expires_at: Any | None,
    actor_role: str,
) -> tuple[ApiKey, str]:
    """Mint a key (reveal-once). Returns ``(ApiKey, plaintext)`` (api-spec §4.5).

    INV-ID-2: verified member required. ``answer_key:read`` is grantable only by
    a workspace admin (api-spec A-4) → 403 otherwise. Key-count quota (max_api_keys)
    enforced at command time (INV-TEN-5). Audit ``tenancy.api_key.created`` (never
    the secret/hash — INV-AUD-3).
    """
    require_verified(actor)  # INV-ID-2

    invalid = [s for s in scopes if s not in KEY_SCOPES]
    if invalid or not scopes:
        from rest_framework.exceptions import ValidationError as DRFValidationError

        raise DRFValidationError({"scopes": [f"Unknown or empty scopes: {invalid or scopes}"]})
    # answer_key:read self-grant by a non-admin → 403 permission-denied (A-4).
    if any(s in ADMIN_ONLY_SCOPES for s in scopes) and actor_role != ROLE_ADMIN:
        raise PermissionDeniedError(
            "Only a workspace admin may grant the answer_key:read scope.",
            required_role=ROLE_ADMIN,
        )

    with transaction.atomic():
        # Command-time key-count quota (INV-TEN-5). Enforcement metering is Phase
        # 11; the key-count cap is synchronously checkable now (api-spec §4.4).
        from rest_framework import status
        from rest_framework.exceptions import APIException

        from config.problems import Slug
        from tenancy.domain.models import WorkspaceQuotas

        quota = WorkspaceQuotas.all_objects.filter(  # tenancy: unscoped — quota row by PK
            workspace=workspace
        ).first()
        active_count = ApiKey.all_objects.filter(  # tenancy: unscoped — count under armed context
            workspace=workspace, revoked_at__isnull=True
        ).count()
        if quota is not None and active_count >= quota.max_api_keys:

            class _QuotaExceeded(APIException):
                status_code = status.HTTP_403_FORBIDDEN
                default_detail = "API-key quota exceeded for this workspace."
                slug = Slug.QUOTA_EXCEEDED

            exc = _QuotaExceeded()
            exc.extensions = {  # type: ignore[attr-defined]
                "quota": "api_keys",
                "limit": quota.max_api_keys,
                "current": active_count,
                "plan": workspace.plan,
            }
            exc.headers = {}  # type: ignore[attr-defined]
            raise exc

        generated = key_crypto.generate_key()
        api_key = ApiKey.objects.create(
            id=uuid.uuid4(),
            workspace=workspace,
            name=name,
            key_prefix=generated.key_prefix,
            key_hash=generated.key_hash,
            last4=generated.last4,
            scopes=list(scopes),
            created_by=actor,
            expires_at=expires_at,
        )
        emit(
            "tenancy.api_key.created",
            actor=actor,
            workspace_id=workspace.id,
            target={
                "type": "api_key",
                "id": str(api_key.id),
                "label": f"{name} ({generated.short_prefix}…{generated.last4})",
            },
            metadata={"scopes": list(scopes), "prefix": generated.short_prefix},
        )
    return api_key, generated.plaintext


def list_keys(*, workspace: Workspace) -> list[ApiKey]:
    """List a workspace's keys (api-spec §4.5; the ``key`` field never appears).

    # tenancy: unscoped — the view has armed the workspace context already.
    """
    return list(
        ApiKey.all_objects.filter(workspace=workspace).order_by("-created_at")
    )


def get_key(*, workspace: Workspace, api_key_id: uuid.UUID) -> ApiKey | None:
    """A single key in ``workspace`` (404-masked by the caller if absent).

    # tenancy: unscoped — explicit workspace filter; foreign id returns None → 404.
    """
    return ApiKey.all_objects.filter(workspace=workspace, id=api_key_id).first()


def revoke_key(
    *, workspace: Workspace, api_key_id: uuid.UUID, actor: User, actor_role: str
) -> None:
    """Revoke a key (SEC-KEY-5): DB state + synchronous Redis write before 204.

    Permitted to the key's creator or any admin. Idempotent on an already-revoked
    key. The Redis write is synchronous and precedes the response so the key is
    rejected < 1 s platform-wide. On Redis failure: DB truth still commits, a
    degraded audit event is written, and the < 1 s contract degrades to the 60 s
    active-TTL bound (SEC-KEY-6) — fail closed to slower, never to allow.
    """
    with transaction.atomic():
        api_key = (
            ApiKey.all_objects.select_for_update()  # tenancy: unscoped — locks the key row
            .filter(workspace=workspace, id=api_key_id)
            .first()
        )
        if api_key is None:
            from config.problems import NotFoundError

            raise NotFoundError()  # foreign/absent key → 404 (W-3 masking)
        if api_key.created_by_id != actor.id and actor_role != ROLE_ADMIN:
            # Creator or admin only (api-spec #24). Within own workspace → 403.
            raise PermissionDeniedError(
                "Only the key's creator or a workspace admin may revoke it.",
                required_role=ROLE_ADMIN,
            )
        if api_key.revoked_at is not None:
            return  # idempotent no-op (already revoked) → caller returns 204

        api_key.revoked_at = timezone.now()
        api_key.revoked_by = actor
        api_key.save(update_fields=["revoked_at", "revoked_by"])

        # Synchronous Redis revocation write BEFORE the response (SEC-KEY-5).
        try:
            revocation_cache.put_revoked(api_key.key_prefix)
        except revocation_cache.RevocationCacheError:
            # DB truth holds; record the degraded path (SEC-KEY-6). A Celery retry
            # of the cache write lands with the maintenance queue (Phase 11).
            emit(
                "tenancy.api_key.revocation_cache_degraded",
                actor=actor,
                workspace_id=workspace.id,
                target={"type": "api_key", "id": str(api_key.id), "label": api_key.key_prefix},
                metadata={"prefix": api_key.key_prefix},
            )
        emit(
            "tenancy.api_key.revoked",
            actor=actor,
            workspace_id=workspace.id,
            target={
                "type": "api_key",
                "id": str(api_key.id),
                "label": f"{api_key.name} ({api_key.key_prefix}…{api_key.last4})",
            },
            metadata={"revoked_by_role": actor_role},
        )


def revoke_all_workspace_keys(*, workspace: Workspace, actor: User) -> None:
    """Revoke every active key of ``workspace`` (INV-TEN-6 cascade; SEC-KEY-8).

    # tenancy: unscoped — workspace-deletion cascade revokes all the ws's keys.
    """
    active = ApiKey.all_objects.select_for_update().filter(
        workspace=workspace, revoked_at__isnull=True
    )
    now = timezone.now()
    for api_key in active:
        api_key.revoked_at = now
        api_key.revoked_by = actor
        api_key.save(update_fields=["revoked_at", "revoked_by"])
        try:
            revocation_cache.put_revoked(api_key.key_prefix)
        except revocation_cache.RevocationCacheError:
            pass  # DB truth holds; the 60 s active-TTL bounds staleness (SEC-KEY-6)


def verify_key(presented: str) -> VerifiedKey:
    """Verify a presented key string; raise 401 ``invalid-api-key`` on any fault.

    Path (security §3.2.3): parse + env-token check (SEC-KEY-2) → Redis revocation
    cache → constant-time hash compare → derived-state (revoked/expired) check.
    No state oracle: every failure is the single ``invalid-api-key`` 401 (A-3).
    last-used is write-behind (SEC-KEY-9).
    """
    parsed = key_crypto.parse_key(presented)
    if parsed is None:
        raise InvalidApiKey()
    # SEC-KEY-2: env token must match the server's environment.
    if parsed.env != key_crypto.env_token():
        raise InvalidApiKey()

    cached = revocation_cache.get_state(parsed.key_prefix)
    if cached == revocation_cache.STATE_REVOKED:
        raise InvalidApiKey()

    if isinstance(cached, revocation_cache.CachedKeyState):
        # Cache hit (active): constant-time hash compare against the cached hash.
        if not key_crypto.keys_match(presented, cached.key_hash):
            raise InvalidApiKey()
        verified = VerifiedKey(
            api_key_id=uuid.UUID(cached.api_key_id),
            workspace_id=uuid.UUID(cached.workspace_id),
            scopes=cached.scopes,
        )
        revocation_cache.touch_last_used(verified.api_key_id)
        return verified

    # Cache miss → consult the database by prefix (fail to slower, SEC-KEY-7).
    # Arm the api_keys Class K auth-bootstrap GUC (security §3.2): the runtime
    # NOBYPASSRLS role can only read the single row whose prefix matches the
    # presented credential, so this lookup works before any workspace context
    # exists without weakening RLS for foreign keys. Cleared immediately after.
    guc.set_api_key_prefix_guc(parsed.key_prefix)
    try:
        api_key = ApiKey.all_objects.filter(  # tenancy: prefix lookup precedes ws context (Class K)
            key_prefix=parsed.key_prefix
        ).first()
    finally:
        guc.set_api_key_prefix_guc(None)
    if api_key is None or not key_crypto.keys_match(presented, api_key.key_hash):
        raise InvalidApiKey()
    if not api_key.is_active():
        # Revoked or expired (derived state); cache the revocation if revoked.
        if api_key.revoked_at is not None:
            try:
                revocation_cache.put_revoked(api_key.key_prefix)
            except revocation_cache.RevocationCacheError:
                pass
        raise InvalidApiKey()

    # Warm the active cache (TTL 60 s) for subsequent verifications.
    revocation_cache.put_active(
        api_key.key_prefix,
        revocation_cache.CachedKeyState(
            api_key_id=str(api_key.id),
            workspace_id=str(api_key.workspace_id),
            key_hash=api_key.key_hash,
            scopes=list(api_key.scopes),
        ),
    )
    revocation_cache.touch_last_used(api_key.id)
    return VerifiedKey(
        api_key_id=api_key.id, workspace_id=api_key.workspace_id, scopes=list(api_key.scopes)
    )


def key_info(verified: VerifiedKey) -> dict[str, Any]:
    """The ``GET /auth/key-info`` introspection body (phase doc §27).

    # tenancy: unscoped — looks up the key by id for its own prefix/created info.
    """
    # Arm the key's own workspace so the Class K by-id read is admitted under the
    # NOBYPASSRLS runtime role (the key-info surface authenticates by key, not by
    # a workspace route, so nothing else armed the context). The workspace id is
    # the verified key's own — reads nothing foreign.
    guc.set_request_gucs(user_id=None, workspace_id=verified.workspace_id)
    api_key = ApiKey.all_objects.filter(id=verified.api_key_id).first()
    prefix = api_key.key_prefix if api_key is not None else ""
    return {
        "api_key_id": str(verified.api_key_id),
        "workspace_id": str(verified.workspace_id),
        "prefix": prefix.split("_")[-1] if prefix else "",
        "scopes": list(verified.scopes),
    }


def caller_role(*, workspace_id: uuid.UUID, user: User) -> str | None:
    """The caller's role in ``workspace_id`` (None if not a member).

    # tenancy: unscoped — role resolution by (workspace, user) keys.
    """
    membership = Membership.all_objects.filter(
        workspace_id=workspace_id, user=user
    ).first()
    return membership.role if membership is not None else None
