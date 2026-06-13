"""The admin-readable audit query path (INV-AUD-4, §10.4).

``read_workspace_audit_log`` is what the tenancy audit-log endpoint
(``GET /workspaces/{id}/audit-log``) imports lazily:

    from audit.application.reader import read_workspace_audit_log
    entries = read_workspace_audit_log(
        workspace_id=..., action=..., action_prefix=..., actor_id=...
    )

It returns a list of plain dicts in the shape the tenancy
``AuditEntrySerializer`` consumes (api-specification §4.14):

    {audit_id, occurred_at, actor:{type,id[,email]}, workspace_id,
     action, target:{type,id,label}, metadata, request_id}

Binding scoping rules:

* **Workspace-scoped (INV-AUD-4).** Only rows whose ``workspace_id`` equals the
  requested workspace are returned. Account-level rows (``workspace_id IS NULL``)
  are **never** served here — they are visible only to the account owner via
  operator tooling (§10.4); a console surface for them is deliberately not in v1.
* **Ordering.** ``occurred_at`` descending (R-6 / api-spec §4.14).
* **Filters.** ``action`` (exact), ``action_prefix`` (e.g. ``streams.``),
  ``actor_id`` (matches the acting user *or* api-key id).

Read-only: this module never writes. Append-only-ness (INV-AUD-1) is preserved by
there being no update/delete code anywhere in the app.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db.models import QuerySet

from audit.domain.models import ACTOR_API_KEY, ACTOR_USER, AuditLog


def _emails_for(user_ids: set[UUID]) -> dict[UUID, str]:
    """Resolve user emails for the ``actor.email`` field (api-spec §4.14).

    Uses the configured auth user model (``get_user_model()``) rather than
    importing ``identity`` directly — a framework lookup, not a cross-app import
    (preserves the import-linter contract). Best-effort: a missing/renamed field
    or absent user simply omits the email.
    """
    if not user_ids:
        return {}
    user_model = get_user_model()
    try:
        rows = user_model._default_manager.filter(pk__in=user_ids).values_list("pk", "email")
    except Exception:
        return {}
    return {pk: email for pk, email in rows if email}


def _actor_dict(entry: AuditLog, emails: dict[UUID, str]) -> dict[str, Any]:
    if entry.actor_type == ACTOR_USER and entry.actor_user_id is not None:
        actor: dict[str, Any] = {"type": ACTOR_USER, "id": str(entry.actor_user_id)}
        email = emails.get(entry.actor_user_id)
        if email:
            actor["email"] = email
        return actor
    if entry.actor_type == ACTOR_API_KEY and entry.actor_api_key_id is not None:
        return {"type": ACTOR_API_KEY, "id": str(entry.actor_api_key_id)}
    return {"type": entry.actor_type}


def _target_dict(entry: AuditLog) -> dict[str, Any]:
    """Reconstruct the ``{type,id,label}`` target ref the API exposes.

    ``label`` was folded into metadata as ``target_label`` by the writer (it is a
    non-secret human reference, e.g. a key ``prefix…last4``); surface it back here
    and keep it out of the metadata blob the client also receives.
    """
    target: dict[str, Any] = {"type": entry.target_type, "id": entry.target_id}
    label = entry.metadata.get("target_label") if isinstance(entry.metadata, dict) else None
    if label:
        target["label"] = str(label)
    return target


def _client_metadata(entry: AuditLog) -> dict[str, Any]:
    """Metadata for the client, minus the internal ``target_label`` echo."""
    if not isinstance(entry.metadata, dict):
        return {}
    return {k: v for k, v in entry.metadata.items() if k != "target_label"}


def _serialize(entry: AuditLog, emails: dict[UUID, str]) -> dict[str, Any]:
    request_id = f"req_{entry.request_id}" if entry.request_id else None
    return {
        "audit_id": str(entry.audit_id),
        "occurred_at": entry.occurred_at,
        "actor": _actor_dict(entry, emails),
        "workspace_id": entry.workspace_id,
        "action": entry.action,
        "target": _target_dict(entry),
        "metadata": _client_metadata(entry),
        "request_id": request_id,
    }


def read_workspace_audit_log(
    *,
    workspace_id: UUID,
    action: str | None = None,
    action_prefix: str | None = None,
    actor_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a workspace's audit entries, newest first (INV-AUD-4, §4.14).

    Account-level (NULL-workspace) rows are excluded by the ``workspace_id``
    equality filter. ``limit`` caps the page (cursor pagination over the
    ``occurred_at`` order lands with the shared cursor infra in a later phase;
    the endpoint currently returns one bounded page with ``next_cursor: null``).
    """
    qs: QuerySet[AuditLog] = AuditLog.objects.filter(workspace_id=workspace_id)
    if action:
        qs = qs.filter(action=action)
    if action_prefix:
        qs = qs.filter(action__startswith=action_prefix)
    if actor_id:
        actor_uuid = _coerce_actor_id(actor_id)
        if actor_uuid is None:
            return []  # malformed actor filter ⇒ no matches (not an error)
        qs = qs.filter(actor_user_id=actor_uuid) | qs.filter(actor_api_key_id=actor_uuid)
    entries = list(qs.order_by("-occurred_at")[:limit])

    user_ids = {e.actor_user_id for e in entries if e.actor_user_id is not None}
    emails = _emails_for(user_ids)
    return [_serialize(e, emails) for e in entries]


def _coerce_actor_id(actor_id: str) -> UUID | None:
    try:
        return UUID(str(actor_id))
    except (ValueError, TypeError):
        return None
