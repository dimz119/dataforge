"""Layer 2 DDL: Postgres Row-Level Security for ``audit_log`` (Class A).

``audit_log`` is a *hybrid* tenant table: workspace-scoped entries carry a
``workspace_id`` and belong to that workspace; account-level entries carry
``workspace_id IS NULL`` and belong to the acting user (INV-AUD-4). Neither the
Class T (plain ``workspace_id = app_workspace_id()``) nor the Class M/W policies
fit — the audit table has its own **Class A** policy (database-schema §9.5):

* ``audit_read``  — a row is visible if it is the active workspace's row, OR it is
  an account-level row owned by the current user. Account rows are therefore
  *operator-only* in MVP (no console surface reads with a NULL workspace context),
  satisfying §10.4 / INV-AUD-4.
* ``audit_insert`` — the writer may insert a workspace row for the active
  workspace, an account-level row for the current user, or a ``system`` row.

The grant matrix (database-schema §9.2) gives the runtime role ``SELECT, INSERT``
only — there is deliberately **no** ``UPDATE``/``DELETE`` policy, so append-only
is enforced at the policy layer too (security §10.2). The null-safe accessors
``app_workspace_id()`` / ``app_user_id()`` are created by the tenancy migration's
``CreateGucAccessors`` (database-schema §9.3); this migration depends on it.

On non-Postgres backends (the SQLite test DB) the SQL is a no-op — RLS is a
Postgres construct; the CI raw-SQL probes carry the guarantee there.

This operation is intentionally distinct from tenancy's ``EnableRowLevelSecurity``
(Classes T/W/M): ``audit.AuditLog`` is listed in ``tenancy_exempt`` (it is hybrid,
not a Class T model), so the ``check_tenancy`` guard does not require the tenancy
marker op here — Audit owns its own RLS enforcement (database-schema §9.5 Class A).
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

_TABLE = "audit_log"

# database-schema §9.5 Class A.
_ENABLE = f"""
ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON "{_TABLE}" FOR SELECT
    USING (workspace_id = app_workspace_id()
           OR (workspace_id IS NULL AND actor_user_id = app_user_id()));
CREATE POLICY audit_insert ON "{_TABLE}" FOR INSERT
    WITH CHECK (workspace_id = app_workspace_id()
                OR (workspace_id IS NULL AND actor_user_id = app_user_id())
                OR actor_type = 'system');
"""

_DISABLE = f"""
DROP POLICY IF EXISTS audit_insert ON "{_TABLE}";
DROP POLICY IF EXISTS audit_read ON "{_TABLE}";
ALTER TABLE "{_TABLE}" NO FORCE ROW LEVEL SECURITY;
ALTER TABLE "{_TABLE}" DISABLE ROW LEVEL SECURITY;
"""


class EnableAuditRowLevelSecurity(migrations.RunSQL):
    """Enable + force RLS on ``audit_log`` with the Class A read/insert policies.

    Ships in the same migration as the table (M-6). The ``model_label`` is carried
    on the instance for symmetry with tenancy's marker op and for introspection;
    the ``check_tenancy`` guard treats ``audit.AuditLog`` as exempt (hybrid §9.5),
    so this op is Audit's own enforcement, not the tenant marker.
    """

    def __init__(self, *, model_label: str = "audit.AuditLog") -> None:
        self.table = _TABLE
        self.policy_class = "A"
        self.model_label = model_label
        super().__init__(sql=_ENABLE, reverse_sql=_DISABLE, elidable=False)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: Any,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return  # RLS is Postgres-only; no-op on the SQLite test DB
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self,
        app_label: str,
        schema_editor: Any,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self) -> str:
        return f"Enable+force RLS (class A) on {self.table}"
