"""Custom migration operations for the partitioned ledger (database-schema §5.5).

Django's ``CreateModel`` cannot emit a ``PARTITION BY RANGE`` parent, so the
ledger table is created by :class:`CreateLedgerParent` on Postgres (raw §5.5 DDL +
the first daily partitions, each with the §5.5 index template and §9.7 RLS
template). On the SQLite unit lane — where partitioning/RLS are no-ops — it falls
back to a plain ``CREATE TABLE`` so the ORM-backed unit tests have a real table.

The model itself is registered via ``migrations.CreateModel`` inside a
``SeparateDatabaseAndState`` so the ORM/state knows it, while the *database* side
is this op (state side is the CreateModel, database side is suppressed for the
parent and replaced here).

:class:`EnableLedgerRowLevelSecurity` subclasses tenancy's
:class:`~tenancy.infra.rls.EnableRowLevelSecurity` so the ``check_tenancy`` guard
recognizes the ledger table as RLS-covered (it scans for that op type), while
running the enable/force/policy on the partitioned *parent* (policies attach to
the parent; §9.7) only on Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

from generation.infra import partitions

# A plain non-partitioned table for the SQLite unit lane (ORM-backed tests).
_SQLITE_PARENT = f"""
CREATE TABLE "{partitions.LEDGER_TABLE}" (
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "workspace_id" char(32) NOT NULL,
    "stream_id" char(32) NOT NULL,
    "shard_id" integer NOT NULL,
    "sequence_no" bigint NOT NULL,
    "event_id" char(32) NOT NULL,
    "event_type" text NOT NULL,
    "occurred_at" datetime NOT NULL,
    "emitted_at" datetime NOT NULL,
    "envelope" text NOT NULL
);
"""
_SQLITE_DROP = f'DROP TABLE IF EXISTS "{partitions.LEDGER_TABLE}";'


class CreateLedgerParent(migrations.RunSQL):
    """Create the ledger table: partitioned parent on PG, plain table on SQLite.

    On Postgres also pre-creates the first daily partitions (today + 3 ahead,
    §8.1) so a freshly migrated DB can accept writes before the partition manager
    (Phase 5 beat task) runs; each partition carries the §5.5 indexes and §9.7
    RLS template. The partition manager keeps the window rolling thereafter.
    """

    def __init__(self) -> None:
        super().__init__(sql="-- ledger parent (see database_forwards)", elidable=False)

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        cursor = schema_editor.connection.cursor()
        if schema_editor.connection.vendor != "postgresql":
            cursor.execute(_SQLITE_PARENT)
            return
        cursor.execute(partitions.create_ledger_parent_sql())
        today = datetime.now(UTC).date()
        partitions.ensure_partitions(cursor, start=today, days_ahead=3)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        cursor = schema_editor.connection.cursor()
        if schema_editor.connection.vendor != "postgresql":
            cursor.execute(_SQLITE_DROP)
            return
        cursor.execute(partitions.drop_ledger_parent_sql())

    def describe(self) -> str:
        return "Create ground_truth_ledger (partitioned parent on PostgreSQL)"
