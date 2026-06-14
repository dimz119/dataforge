"""LedgerSink port adapter — batched canonical-envelope append to the ledger
(database-schema §5.5; behavior-engine §10; engine port
:class:`dataforge_engine.ports.LedgerSink`).

The engine appends each pass's batch here *before* any downstream stage
(INV-GEN-5). This adapter writes the **full internal envelope** (all 20 fields +
``_df`` with ``_df.canonical = true``) as JSONB, plus the §5.5 extracted scalar
columns, in **batched multi-row inserts** (one statement per pass, the §5.5
"batched COPY/multi-row inserts … one transaction per (stream, shard) tick
batch"). Append is idempotent on ``(stream_id, shard_id, sequence_no, emitted_at)``
via ``ON CONFLICT DO NOTHING`` so a crash + deterministic re-generation
re-appends harmlessly. ``workspace_id`` is denormalized onto every row (C-8).

Writes go through the Django ``default`` connection — which the runtime carries as
the ``dataforge_app`` NOBYPASSRLS role — so RLS bites (the row's ``workspace_id``
must equal the armed ``app.workspace_id`` GUC). The caller arms the workspace
context (and thus the GUC) before driving the engine.

The on-conflict clause is Postgres-specific; on the SQLite unit lane the adapter
falls back to ``INSERT OR IGNORE`` so ORM-backed unit tests exercise the same path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.db import connection

from dataforge_engine.envelope import canonical_serialize_str

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dataforge_engine.envelope import InternalEnvelope

__all__ = ["LedgerSink"]

_TABLE = "ground_truth_ledger"
_COLUMNS = (
    "workspace_id",
    "stream_id",
    "shard_id",
    "sequence_no",
    "event_id",
    "event_type",
    "occurred_at",
    "emitted_at",
    "envelope",
)


def _uuid_param(value: str) -> str:
    """Format a UUID string for the active DB's raw-SQL parameter binding.

    Postgres' ``uuid`` type accepts the canonical dashed form; the SQLite unit
    lane stores ``UUIDField`` as 32-char hex (no dashes, Django's converter, which
    the raw cursor bypasses), so the value must match for the ORM query to find
    the row. Returns the dashed form on Postgres, the 32-hex form on SQLite.
    """
    import uuid as _uuid

    parsed = _uuid.UUID(value)
    return str(parsed) if connection.vendor == "postgresql" else parsed.hex


def _parse_ts(value: str) -> datetime:
    """RFC-3339 envelope timestamp string → tz-aware UTC ``datetime``.

    The envelope serializes ``occurred_at``/``emitted_at`` as RFC-3339 strings
    (event-model canonical clocks); the ledger's extracted columns parse them back
    so they agree exactly with the serialized envelope values.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class LedgerSink:
    """Concrete :class:`dataforge_engine.ports.LedgerSink` over Postgres (§5.5).

    Bound to one (workspace, stream) at construction; the engine supplies
    ``shard_id``/``sequence_no`` per envelope.
    """

    def __init__(self, *, workspace_id: str) -> None:
        self._workspace_id = workspace_id
        self.rows_written = 0

    def append(self, envelopes: Sequence[InternalEnvelope]) -> None:
        """Durably append a batch of canonical envelopes in order (one statement)."""
        if not envelopes:
            return
        ws_id = _uuid_param(self._workspace_id)
        params: list[Any] = []
        for env in envelopes:
            params.extend(
                (
                    ws_id,
                    _uuid_param(str(env["stream_id"])),
                    int(env["shard_id"]),
                    int(env["sequence_no"]),
                    _uuid_param(str(env["event_id"])),
                    str(env["event_type"]),
                    _parse_ts(str(env["occurred_at"])),
                    _parse_ts(str(env["emitted_at"])),
                    # S-2 canonical serialization (byte-stable; renders Decimal as
                    # its literal digits, the envelope library's job, not json's).
                    canonical_serialize_str(env),
                )
            )
        self._execute(len(envelopes), params)
        self.rows_written += len(envelopes)

    def _execute(self, n: int, params: list[Any]) -> None:
        is_pg = connection.vendor == "postgresql"
        cast = "::jsonb" if is_pg else ""
        placeholder = f"(%s, %s, %s, %s, %s, %s, %s, %s, %s{cast})"
        values_sql = ", ".join([placeholder] * n)
        cols = ", ".join(_COLUMNS)
        if is_pg:
            sql = (
                f"INSERT INTO {_TABLE} ({cols}) VALUES {values_sql} "
                f"ON CONFLICT (stream_id, shard_id, sequence_no, emitted_at) DO NOTHING"
            )
        else:
            sql = f"INSERT OR IGNORE INTO {_TABLE} ({cols}) VALUES {values_sql}"
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
