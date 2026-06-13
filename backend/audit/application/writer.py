"""The audit writer — the single INSERT site for ``audit_log`` (INV-AUD-1..3).

``record_audit`` is the function identity and tenancy import lazily from their
``application.audit.emit`` seams:

    from audit.application.writer import record_audit
    record_audit(action=..., actor=..., workspace_id=..., target=..., metadata=...)

Contract held by this module (all binding):

* **INV-AUD-2 — same transaction.** The write is a plain ORM ``create`` on the
  current connection; it therefore *joins the caller's open atomic block* (the
  mutation's ``transaction.atomic`` / ``ATOMIC_REQUESTS``). We never open a new
  connection or a nested ``atomic`` of our own — if the caller's transaction
  rolls back, the audit row is dropped with it (and vice versa). An action whose
  audit write raises does not commit (SEC-AUD-2).
* **INV-AUD-3 — no secrets.** ``metadata`` and the ``target`` ref are passed
  through ``infra.sanitize.scrub`` which drops secret-shaped keys defensively,
  even though callers already pass references/prefixes only.
* **request_id stamping.** The originating request's correlation id (bound by the
  request-id middleware into structlog contextvars) is read and stored
  (database-schema §7.1 ``request_id``); NULL outside a request (Celery/system).
* **append-only.** This is the *only* place a row is created; there is no update
  or delete path anywhere in the app (INV-AUD-1).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from django.db import connection

from audit.domain.models import (
    ACTOR_API_KEY,
    ACTOR_SYSTEM,
    ACTOR_USER,
    AuditLog,
)
from audit.infra.request_id import current_request_id
from audit.infra.sanitize import scrub

logger = structlog.get_logger(__name__)


def _arm_audit_insert_gucs(
    *, workspace_id: UUID | None, actor_type: str, actor_user_id: UUID | None
) -> None:
    """Arm the transaction-local GUCs the Class A ``audit_insert`` policy reads.

    The runtime connects as ``dataforge_app`` (NOBYPASSRLS, SEC-TEN-2), so the
    audit ``WITH CHECK`` (audit.infra.rls) is enforced: a workspace row needs
    ``app.workspace_id`` to match its ``workspace_id``; an account-level user row
    (``workspace_id IS NULL``) needs ``app.user_id`` to match its ``actor_user_id``;
    a ``system`` row is unconditionally allowed.

    Authenticated requests already have these armed by the workspace-context
    middleware (security §4.1/§9.4). This arms them defensively for the trusted
    *unauthenticated* account-level writes too — signup/login/password-reset emit
    ``identity.*`` rows with no session GUC set — mirroring the §9.4 "arm the
    app-generated id before the INSERT so WITH CHECK passes" pattern used by
    workspace creation. The values come from the trusted caller, not request input,
    and are set ``is_local => true`` so they die with the caller's transaction.

    No-op off Postgres (the SQLite unit lane has no GUCs / no RLS).
    """
    if connection.vendor != "postgresql" or actor_type == ACTOR_SYSTEM:
        return
    with connection.cursor() as cursor:
        if workspace_id is not None:
            cursor.execute(
                "SELECT set_config('app.workspace_id', %s, true)", [str(workspace_id)]
            )
        elif actor_user_id is not None:
            # Account-level row: the policy keys on app.user_id = actor_user_id.
            # The actor of an account-level row is always the acting user, so
            # setting app.user_id to it is correct whether or not the middleware
            # already armed the same value for an authenticated request.
            cursor.execute(
                "SELECT set_config('app.user_id', %s, true)",
                [str(actor_user_id)],
            )


def _coerce_uuid(value: Any) -> UUID | None:
    """Best-effort UUID coercion for ids arriving as UUID|str|None."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _resolve_actor(actor: Any) -> tuple[str, UUID | None, UUID | None]:
    """Map the caller's ``actor`` to (actor_type, actor_user_id, actor_api_key_id).

    Callers pass a ``User`` instance, an API-key principal (has ``api_key_id``),
    or ``None``/a ``"system"`` sentinel for system actions (quota/idle pauses,
    expiries). Duck-typed so Audit imports no other app's classes (import-linter
    cross-app contract); the actor-presence CHECK (§7.1) is satisfied by mapping
    each case to its matching id field.
    """
    if actor is None or actor == ACTOR_SYSTEM:
        return ACTOR_SYSTEM, None, None
    # API-key principal: discriminated by an ``api_key_id`` attribute.
    api_key_id = _coerce_uuid(getattr(actor, "api_key_id", None))
    if api_key_id is not None:
        return ACTOR_API_KEY, None, api_key_id
    # User-like actor: prefer ``pk`` (Django models), fall back to ``id``.
    user_id = _coerce_uuid(getattr(actor, "pk", None) or getattr(actor, "id", None))
    if user_id is not None:
        return ACTOR_USER, user_id, None
    # Unidentifiable actor → record as system rather than violate the CHECK.
    logger.warning("audit_actor_unresolved", actor_type=type(actor).__name__)
    return ACTOR_SYSTEM, None, None


def _resolve_target(target: Any) -> tuple[str, str]:
    """Map the caller's ``target`` ref to (target_type, target_id).

    Callers pass ``{"type": ..., "id": ..., "label": ...}`` (the label is folded
    into metadata for the read surface). ``None`` ⇒ the action has no specific
    object (e.g. a login) → an empty-but-non-null ref, satisfying the NOT NULL
    columns (§7.1).
    """
    if isinstance(target, dict):
        target_type = str(target.get("type", "") or "")
        target_id = str(target.get("id", "") or "")
        return target_type, target_id
    if target is None:
        return "", ""
    return type(target).__name__, str(target)


def record_audit(
    *,
    action: str,
    actor: Any,
    workspace_id: Any | None = None,
    target: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one immutable audit entry in the caller's current transaction.

    Returns the created row (callers ignore it; returned for test assertions).
    Raises only if the underlying INSERT fails — by design, so a failed audit
    write aborts the enclosing transaction (SEC-AUD-2 / INV-AUD-2).
    """
    actor_type, actor_user_id, actor_api_key_id = _resolve_actor(actor)
    target_type, target_id = _resolve_target(target)

    # INV-AUD-3: never persist secret-shaped values, defensively. The target
    # ``label`` (a human reference, e.g. a key prefix+last4) is folded in after
    # scrubbing so it survives while real secrets cannot.
    clean_metadata: dict[str, Any] = scrub(dict(metadata or {}))
    if isinstance(target, dict) and target.get("label"):
        clean_metadata.setdefault("target_label", str(target["label"]))

    # Arm the Class A audit_insert GUCs so the WITH CHECK passes under the
    # NOBYPASSRLS runtime role (SEC-TEN-2). Joins the caller's open transaction;
    # set_config(is_local => true) dies with it.
    ws_uuid = _coerce_uuid(workspace_id)
    _arm_audit_insert_gucs(
        workspace_id=ws_uuid, actor_type=actor_type, actor_user_id=actor_user_id
    )

    return AuditLog.objects.create(
        action=action,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        actor_api_key_id=actor_api_key_id,
        workspace_id=ws_uuid,
        target_type=target_type,
        target_id=target_id,
        metadata=clean_metadata,
        request_id=current_request_id(),
    )
