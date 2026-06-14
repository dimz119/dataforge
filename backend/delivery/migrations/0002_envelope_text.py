# Store ``event_buffer.envelope`` as TEXT, not jsonb (delivery-channels §4.2 / BW-5).
#
# The delivered envelope is stored as CANONICAL JSON whose exact bytes are the
# cross-channel identity contract (S-3: a consumer migrating REST→Kafka must see
# byte-identical envelopes). ``jsonb`` reorders object keys and normalizes numbers,
# which alters those bytes (e.g. nested ``entity_refs`` keys come back reordered),
# breaking S-3. ``text`` preserves the canonical string verbatim; the strip already
# pinned the 20-key shape, so the column is a byte-faithful blob, not a queryable doc.
#
# SeparateDatabaseAndState: the STATE side flips the model field JSONField→TextField
# (the SQLite unit table was already ``text``); the DATABASE side runs an ALTER on
# Postgres only (the partitioned parent cascades to its partitions). On SQLite the
# DB side is a no-op (the column is already TEXT).
from __future__ import annotations

from typing import Any

from django.db import migrations, models
from django.db.migrations.state import ProjectState


class AlterEnvelopeToText(migrations.RunSQL):
    """ALTER ``event_buffer.envelope`` jsonb → text on Postgres (no-op off PG)."""

    def __init__(self) -> None:
        super().__init__(
            sql='ALTER TABLE event_buffer ALTER COLUMN envelope TYPE text USING envelope::text;',
            reverse_sql='ALTER TABLE event_buffer ALTER COLUMN envelope TYPE jsonb USING envelope::jsonb;',
            elidable=False,
        )

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return  # SQLite column is already TEXT
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self) -> str:
        return "Alter event_buffer.envelope jsonb -> text (canonical byte-identity)"


class Migration(migrations.Migration):
    dependencies = [
        ("delivery", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="eventbuffer",
                    name="envelope",
                    field=models.TextField(),
                ),
            ],
            database_operations=[AlterEnvelopeToText()],
        ),
    ]
