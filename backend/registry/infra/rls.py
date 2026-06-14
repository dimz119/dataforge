"""Layer 2 DDL: Postgres Row-Level Security for the registry tables (Class H).

``schema_subjects`` and ``schema_versions`` are *hybrid* catalog tables
(database-schema §4.4-4.5, §9.5): rows with ``workspace_id IS NULL`` are
platform-global builtin subjects readable by **every** authenticated principal
(INV-REG-4 — builtin subjects must resolve for every workspace's envelopes);
rows with a non-null ``workspace_id`` are tenant-owned and isolated like a Class T
table. Neither Class T nor Class M/W fits — these tables carry the **Class H**
policy set (database-schema §9.5):

* ``catalog_read``  — global rows (``workspace_id IS NULL``) OR the active
  workspace's own rows are readable.
* ``catalog_write`` (INSERT) / ``catalog_upd`` (UPDATE) / ``catalog_del``
  (DELETE) — tenant rows only: a workspace may only write its own subjects. Global
  rows are written **exclusively** by the ``dataforge_maintenance`` loader role
  during builtin sync + manifest publication (database-schema §9.6) — the runtime
  ``dataforge_app`` role can never INSERT a global (NULL-workspace) row, which is
  the property that keeps the builtin catalog read-only to tenants.

The null-safe accessors ``app_workspace_id()`` / ``app_user_id()`` are created by
the tenancy migration's ``CreateGucAccessors`` (database-schema §9.3); this
migration depends on it. On non-Postgres backends (the SQLite test DB) the SQL is
a no-op — RLS is a Postgres construct; the CI raw-SQL probes carry the guarantee
there.

This operation is intentionally distinct from tenancy's ``EnableRowLevelSecurity``
(Classes T/W/M/K): the registry tables are listed in ``tenancy_exempt`` (they are
hybrid, not Class T), so the ``check_tenancy`` guard does not require the tenancy
marker op here — Registry owns its own RLS enforcement (database-schema §9.5
Class H).
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

# database-schema §9.5 Class H — global rows world-readable, writes tenant-only.
_ENABLE = """
ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY;
CREATE POLICY catalog_read ON "{t}" FOR SELECT
    USING (workspace_id IS NULL OR workspace_id = app_workspace_id());
CREATE POLICY catalog_write ON "{t}" FOR INSERT
    WITH CHECK (workspace_id = app_workspace_id());
CREATE POLICY catalog_upd ON "{t}" FOR UPDATE
    USING (workspace_id = app_workspace_id())
    WITH CHECK (workspace_id = app_workspace_id());
CREATE POLICY catalog_del ON "{t}" FOR DELETE
    USING (workspace_id = app_workspace_id());
"""

_DISABLE = """
DROP POLICY IF EXISTS catalog_del ON "{t}";
DROP POLICY IF EXISTS catalog_upd ON "{t}";
DROP POLICY IF EXISTS catalog_write ON "{t}";
DROP POLICY IF EXISTS catalog_read ON "{t}";
ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY;
ALTER TABLE "{t}" DISABLE ROW LEVEL SECURITY;
"""


class EnableHybridRowLevelSecurity(migrations.RunSQL):
    """Enable + force RLS on a hybrid (Class H) registry table.

    Ships in the same migration as the table (M-6). The ``model_label`` is carried
    on the instance for symmetry with tenancy's marker op and for introspection;
    the ``check_tenancy`` guard treats the registry models as exempt (hybrid §9.5),
    so this op is Registry's own enforcement, not the tenant marker.
    """

    def __init__(self, *, table: str, model_label: str = "") -> None:
        self.table = table
        self.policy_class = "H"
        self.model_label = model_label
        super().__init__(
            sql=_ENABLE.format(t=table),
            reverse_sql=_DISABLE.format(t=table),
            elidable=False,
        )

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
        return f"Enable+force RLS (class H) on {self.table}"
