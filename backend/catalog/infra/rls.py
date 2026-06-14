"""Layer 2 DDL: Postgres Row-Level Security for the catalog tables.

The Scenario Catalog owns three tables (database-schema §4.1-4.3, §9.5):

* ``scenarios`` and ``scenario_definitions`` are **hybrid** (Class H): rows with
  ``workspace_id IS NULL`` are platform-global builtins readable by every
  authenticated principal (INV-CAT-6 — the builtin catalog is shared read-only
  product content); rows with a non-null ``workspace_id`` are tenant-owned (the
  AI-manifest seam, §12). Global rows are written **only** by the
  ``dataforge_maintenance`` loader role during ``sync_builtin_scenarios`` and
  manifest publication (database-schema §9.6); the runtime ``dataforge_app`` role
  can never INSERT a global row, which is what keeps builtins tenant-read-only.
* ``workspace_scenario_configs`` (scenario instances) is a **standard Class T**
  tenant table (non-null ``workspace_id``) — it gets the tenancy
  ``EnableRowLevelSecurity`` op + the ``WorkspaceScoped`` model, not Class H.

This module provides only the Class H operation for the two hybrid tables
(``EnableHybridRowLevelSecurity``); the Class T op for ``workspace_scenario_configs``
comes from ``tenancy.infra.rls`` so the ``check_tenancy`` guard recognizes it.

The null-safe accessors ``app_workspace_id()`` / ``app_user_id()`` are created by
the tenancy migration's ``CreateGucAccessors`` (database-schema §9.3); the
catalog migration depends on it. On non-Postgres backends (the SQLite test DB)
the SQL is a no-op.
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
    """Enable + force RLS on a hybrid (Class H) catalog table.

    Ships in the same migration as the table (M-6). The catalog hybrid tables
    (``scenarios``, ``scenario_definitions``) are listed in ``tenancy_exempt``
    (hybrid §9.5), so the ``check_tenancy`` guard does not require the tenancy
    marker op here — Catalog owns its own RLS for these two tables.
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
