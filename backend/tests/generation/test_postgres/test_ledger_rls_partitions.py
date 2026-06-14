"""RLS + partition-routing tests for the ledger (database-schema §5.5, §9.5/§9.7).

These run in the Postgres lane (``config.settings.test_postgres``) where RLS and
RANGE partitioning are live; they skip on the SQLite unit DB. They prove:

* the ledger carries Class-T RLS — a foreign workspace's GUC sees zero ledger
  rows (the second wall, independent of the scoped manager);
* an unset GUC sees zero rows (default-deny);
* ledger writes route to the daily partition matching the row's ``emitted_at``
  (the partition manager pre-creates today's partition).

The verify agent owns the compose/CI Postgres runtime; structured here so the
RLS-sensitive assertions are isolated from the fast unit lane.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.db import connection

from generation.infra import partitions
from generation.infra.ledger_sink import LedgerSink
from tests.generation.conftest import WorkspaceFixture

pytestmark = pytest.mark.django_db


def _skip_unless_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Ledger RLS + partition routing require PostgreSQL (compose/CI lane).")


def _envelope(
    *, workspace_id: str, stream_id: str, seq: int, emitted: datetime
) -> dict[str, Any]:
    """A minimal internal-envelope-shaped dict for a direct ledger append."""
    occurred = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "envelope_version": "1.0",
        "event_id": str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "stream_id": stream_id,
        "shard_id": 0,
        "scenario_slug": "ecommerce",
        "manifest_version": "1.0.0",
        "event_type": "session_started",
        "schema_ref": {"subject": "ecommerce.session_started", "version": 1},
        "sequence_no": seq,
        "partition_key": "usr_0001",
        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
        "emitted_at": emitted.isoformat().replace("+00:00", "Z"),
        "actor_id": "usr_0001",
        "session_id": None,
        "entity_refs": [],
        "correlation_id": "",
        "causation_id": None,
        "op": None,
        "payload": {"user_id": "usr_0001"},
        "_df": {"canonical": True},
    }


def test_ledger_foreign_guc_sees_zero_rows(gen_ledger_ws: WorkspaceFixture) -> None:
    """Under a foreign workspace GUC, raw SELECTs over the ledger return 0 rows."""
    _skip_unless_postgres()
    ws_id = str(gen_ledger_ws.workspace.id)
    stream_id = str(uuid.uuid4())
    emitted = datetime.now(UTC)
    LedgerSink(workspace_id=ws_id).append(
        [_envelope(workspace_id=ws_id, stream_id=stream_id, seq=1, emitted=emitted)]  # type: ignore[list-item]
    )
    foreign = str(uuid.uuid4())
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [foreign])
        cursor.execute(
            "SELECT count(*) FROM ground_truth_ledger WHERE workspace_id = %s", [ws_id]
        )
        assert cursor.fetchone()[0] == 0


def test_ledger_unset_guc_sees_zero_rows(gen_ledger_ws: WorkspaceFixture) -> None:
    """With the GUC unset, raw SELECTs over the ledger return 0 rows (default-deny)."""
    _skip_unless_postgres()
    ws_id = str(gen_ledger_ws.workspace.id)
    emitted = datetime.now(UTC)
    LedgerSink(workspace_id=ws_id).append(
        [_envelope(workspace_id=ws_id, stream_id=str(uuid.uuid4()), seq=1, emitted=emitted)]  # type: ignore[list-item]
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', '', true)")
        cursor.execute("SELECT count(*) FROM ground_truth_ledger")
        assert cursor.fetchone()[0] == 0


def test_ledger_row_routes_to_emitted_at_partition(gen_ledger_ws: WorkspaceFixture) -> None:
    """A written row lands in the daily partition matching its ``emitted_at``."""
    _skip_unless_postgres()
    ws_id = str(gen_ledger_ws.workspace.id)
    stream_id = str(uuid.uuid4())
    emitted = datetime.now(UTC)
    LedgerSink(workspace_id=ws_id).append(
        [_envelope(workspace_id=ws_id, stream_id=stream_id, seq=1, emitted=emitted)]  # type: ignore[list-item]
    )
    part = partitions.partition_name(emitted.date())
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [ws_id])
        cursor.execute(f"SELECT count(*) FROM {part} WHERE stream_id = %s", [stream_id])
        assert cursor.fetchone()[0] == 1
