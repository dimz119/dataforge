"""Layer-2 DDL: the manifest-version immutability trigger (database-schema §4.2).

Published manifest versions are immutable forever (INV-CAT-1). The app exposes no
update path for ``manifest`` once ``status != 'draft'``; a row-level
``BEFORE UPDATE`` trigger is the DB backstop — it rejects any UPDATE that changes
``manifest``, ``manifest_sha256``, ``version``, or ``builtin`` after publication
(the one DB trigger the schema declares; the catalog is low-volume).
``deprecated`` only flips ``status`` (INV-CAT-5). A draft row may still be edited
in place (re-upload, §10.1) — the trigger only bites once published.

On non-Postgres backends (the SQLite test DB) the SQL is a no-op; immutability is
asserted there by the absence of any mutating code path plus the GUARD tests.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

_TABLE = "scenario_definitions"

_CREATE = f"""
CREATE OR REPLACE FUNCTION catalog_scenario_definitions_immutable()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'draft' AND (
           NEW.manifest        IS DISTINCT FROM OLD.manifest
        OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
        OR NEW.version         IS DISTINCT FROM OLD.version
        OR NEW.builtin         IS DISTINCT FROM OLD.builtin
    ) THEN
        RAISE EXCEPTION
            'published manifest versions are immutable (INV-CAT-1); publish a new version'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scenario_definitions_immutable
    BEFORE UPDATE ON "{_TABLE}"
    FOR EACH ROW EXECUTE FUNCTION catalog_scenario_definitions_immutable();
"""

_DROP = f"""
DROP TRIGGER IF EXISTS scenario_definitions_immutable ON "{_TABLE}";
DROP FUNCTION IF EXISTS catalog_scenario_definitions_immutable();
"""


class PostgresOnlyRunSQL(migrations.RunSQL):
    """A ``RunSQL`` op that runs only on PostgreSQL (no-op on the SQLite unit lane).

    Used for the §4.1/§4.2 regex CHECK constraints, which are Postgres ``~`` regex
    matches not expressible as a Django ``Q``. On SQLite the manifest grammar +
    ``catalog.application`` enforce the same patterns in Python; the DB CHECK is the
    Postgres backstop and must not break the unit test DB (M-6).
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


class InstallManifestImmutability(migrations.RunSQL):
    """Install the BEFORE UPDATE immutability trigger on scenario_definitions."""

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
        return "Install scenario_definitions immutability trigger"
