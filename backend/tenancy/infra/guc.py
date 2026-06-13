"""Layer 2 wiring: the per-request Postgres GUC setter (security §4.2, §9.4).

Sets the transaction-local session variables ``app.workspace_id`` and
``app.user_id`` that the RLS policies read (database-schema §9.3). Uses
``set_config(name, value, is_local => true)`` so the setting dies with the
transaction — mandatory for safety under transaction-mode connection pooling
(§9.3): a pooled connection never leaks context across requests.

The middleware calls ``set_request_gucs`` after authentication, inside the
``ATOMIC_REQUESTS`` transaction; the workspace-creation flow (§9.4) calls
``set_workspace_guc`` *first* (the app-generated id armed before the INSERT so
the ``WITH CHECK`` policy passes).

No-ops on non-Postgres backends (the test suite runs on SQLite) — RLS is a
Postgres construct, so the GUC has no meaning elsewhere. Layer 1 (scoped
managers) and the permanent raw-SQL RLS probes (run against Postgres in CI)
carry the isolation guarantee there.
"""

from __future__ import annotations

import uuid

from django.db import connection


def _is_postgres() -> bool:
    return connection.vendor == "postgresql"


def set_workspace_guc(workspace_id: uuid.UUID | None) -> None:
    """SET LOCAL app.workspace_id for the current transaction (RLS arming)."""
    if not _is_postgres():
        return
    value = str(workspace_id) if workspace_id is not None else ""
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [value])


def set_user_guc(user_id: uuid.UUID | None) -> None:
    """SET LOCAL app.user_id for the current transaction (Class W/M/A policies)."""
    if not _is_postgres():
        return
    value = str(user_id) if user_id is not None else ""
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [value])


def set_api_key_prefix_guc(key_prefix: str | None) -> None:
    """SET LOCAL app.api_key_prefix for the api_keys Class K auth-bootstrap branch.

    ApiKeyAuthentication arms this with the *presented* key's prefix right before
    the by-prefix lookup so the Class K policy (tenancy.infra.rls) admits exactly
    that one row under the NOBYPASSRLS runtime role (SEC-TEN-2) — never any other
    key. Transaction-local; no-op off Postgres.
    """
    if not _is_postgres():
        return
    value = key_prefix or ""
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.api_key_prefix', %s, true)", [value])


def set_request_gucs(
    *, user_id: uuid.UUID | None, workspace_id: uuid.UUID | None
) -> None:
    """Arm both GUCs for a request transaction (security §9.4)."""
    set_user_guc(user_id)
    set_workspace_guc(workspace_id)


def clear_request_gucs() -> None:
    """Reset all request GUCs to empty (fail-closed; defensive end-of-request clear)."""
    set_user_guc(None)
    set_workspace_guc(None)
    set_api_key_prefix_guc(None)
