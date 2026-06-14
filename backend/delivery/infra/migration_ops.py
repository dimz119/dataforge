"""Custom migration operation for the partitioned ``event_buffer`` (database-schema §6.1).

Django's ``CreateModel`` cannot emit a ``PARTITION BY RANGE`` parent, so the buffer
table is created by :class:`CreateBufferParent` on Postgres (raw §6.1 DDL + the
first hourly partitions, each with the §6.1 index template and §9.7 RLS template).
On the SQLite unit lane — where partitioning/RLS are no-ops — it falls back to a
plain ``CREATE TABLE`` so the ORM-backed unit tests have a real table.

The model itself is registered via ``migrations.CreateModel`` inside a
``SeparateDatabaseAndState`` so the ORM/state knows it, while the *database* side
is this op. RLS coverage for the ``check_tenancy`` guard is declared separately in
the migration body via tenancy's :class:`~tenancy.infra.rls.EnableRowLevelSecurity`
(the op the guard scans for); on Postgres that op attaches the policy to the
partitioned *parent* (§9.7) and :class:`CreateBufferParent` applies it to each
created partition too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

from delivery.infra import partitions

# A plain non-partitioned table for the SQLite unit lane (ORM-backed tests).
_SQLITE_PARENT = f"""
CREATE TABLE "{partitions.BUFFER_TABLE}" (
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "workspace_id" char(32) NOT NULL,
    "stream_id" char(32) NOT NULL,
    "partition_ts" datetime NOT NULL,
    "buffer_seq" bigint NOT NULL,
    "event_id" char(32) NOT NULL,
    "event_type" text NOT NULL,
    "occurred_at" datetime NOT NULL,
    "emitted_at" datetime NOT NULL,
    "envelope" text NOT NULL
);
"""
_SQLITE_DROP = f'DROP TABLE IF EXISTS "{partitions.BUFFER_TABLE}";'


class CreateBufferParent(migrations.RunSQL):
    """Create ``event_buffer``: partitioned parent on PG, plain table on SQLite.

    On Postgres also pre-creates the first hourly partitions (this hour + a small
    look-ahead, §8.1) so a freshly migrated DB can accept writes before the
    partition manager (Phase 5 beat task) runs; each partition carries the §6.1
    index and §9.7 RLS template. The partition manager keeps the window rolling.
    """

    def __init__(self) -> None:
        super().__init__(sql="-- buffer parent (see database_forwards)", elidable=False)

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        cursor = schema_editor.connection.cursor()
        if schema_editor.connection.vendor != "postgresql":
            cursor.execute(_SQLITE_PARENT)
            return
        cursor.execute(partitions.create_buffer_parent_sql())
        partitions.ensure_partitions(
            cursor, start=datetime.now(UTC), hours_ahead=partitions.CREATE_AHEAD_HOURS
        )

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        cursor = schema_editor.connection.cursor()
        if schema_editor.connection.vendor != "postgresql":
            cursor.execute(_SQLITE_DROP)
            return
        cursor.execute(partitions.drop_buffer_parent_sql())

    def describe(self) -> str:
        return "Create event_buffer (partitioned parent on PostgreSQL)"
