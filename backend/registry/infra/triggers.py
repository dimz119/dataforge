"""Layer-2 DDL: the registry immutability trigger (database-schema §4.5).

``schema_versions`` rows are immutable forever: streams, ledger rows, buffer
rows, and answer keys reference ``schema_ref`` values indefinitely (PIN-5). The
app exposes no update/delete path, and a ``BEFORE UPDATE OR DELETE`` row trigger
rejects any mutation as the DB backstop (the same trigger pattern as published
``scenario_definitions``, §4.2). The ``dataforge_app`` grant matrix also excludes
the registry immutables from UPDATE/DELETE (§9.1) — defense in depth.

On non-Postgres backends (the SQLite test DB) the SQL is a no-op; immutability is
asserted there by the absence of any mutating code path plus the GUARD tests.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

_TABLE = "schema_versions"

_CREATE = f"""
CREATE OR REPLACE FUNCTION registry_schema_versions_immutable()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'schema_versions rows are immutable (INV-REG-2); register a new version instead'
        USING ERRCODE = 'restrict_violation';
END;
$$;
CREATE TRIGGER schema_versions_immutable
    BEFORE UPDATE OR DELETE ON "{_TABLE}"
    FOR EACH ROW EXECUTE FUNCTION registry_schema_versions_immutable();
"""

_DROP = f"""
DROP TRIGGER IF EXISTS schema_versions_immutable ON "{_TABLE}";
DROP FUNCTION IF EXISTS registry_schema_versions_immutable();
"""


class PostgresOnlyRunSQL(migrations.RunSQL):
    """A ``RunSQL`` op that runs only on PostgreSQL (no-op on the SQLite unit lane).

    Used for the §4.4 subject structural CHECK, a Postgres ``~`` regex match not
    expressible as a Django ``Q``. On SQLite the subject grammar is enforced in
    Python by derivation; the DB CHECK is the Postgres backstop and must not break
    the unit test DB (M-6).
    """

    def __init__(self, *, sql: str, reverse_sql: str) -> None:
        super().__init__(sql=sql, reverse_sql=reverse_sql, elidable=False)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: Any,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
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
        return "Run Postgres-only DDL (no-op on SQLite)"


class InstallSchemaVersionImmutability(migrations.RunSQL):
    """Install the BEFORE UPDATE OR DELETE immutability trigger on schema_versions."""

    def __init__(self) -> None:
        super().__init__(sql=_CREATE, reverse_sql=_DROP, elidable=False)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: Any,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
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
        return "Install schema_versions immutability trigger"
