"""RLS + COPY + partition-routing tests for ``event_buffer`` (database-schema §6.1).

These run in the Postgres lane (``config.settings.test_postgres``) where the binary
``COPY`` write path, hourly RANGE partitioning, and Class-T RLS are live; they skip
on the SQLite unit DB (the logic itself is covered there via the plain-table
fallback in ``tests/delivery/test_buffer_writer``). They prove the Phase-5
buffer-writer obligations that only bite under real Postgres:

* the transactional ``COPY`` path persists the delivered shape (BW-2 / BW-5);
* ``buffer_seq`` is per-stream monotonic via the recovered counter over real rows
  (BW-6 / BW-8);
* the row routes to the hourly partition matching its ``partition_ts`` (§4.3);
* ``event_buffer`` carries Class-T RLS — a foreign workspace GUC sees zero rows,
  and an unset GUC sees zero rows (default-deny, the second wall, §9.5).

The verify agent owns the compose/CI Postgres runtime; structured here so the
RLS-sensitive assertions are isolated from the fast unit lane (Phase-5 CI note).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from django.db import connection

from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
from delivery.infra import partitions
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import make_batch, make_internal_event

pytestmark = pytest.mark.django_db


def _skip_unless_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip(
            "event_buffer COPY/RLS/partitions require PostgreSQL (compose/CI lane)."
        )


@pytest.fixture
def buffer_ws(db: Any) -> Any:
    """A real workspace + armed GUC so the NOBYPASSRLS role's writes pass RLS.

    The production create flow arms ``app.workspace_id`` so the Class-W WITH CHECK
    passes under ``dataforge_app``; we then deliver batches whose ``workspace_id``
    equals this workspace (the engine fixture's envelopes are rewritten to it).
    """
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    admin = User.objects.create_user(
        email="buffer-admin@example.com", password="pw-correct-horse"
    )
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Buffer Lab", slug=None)
    ws_context.activate(workspace.id)
    return workspace


def _events_for(workspace_id: str, stream_id: str, n: int) -> list[dict[str, Any]]:
    """N internal envelopes re-attributed to ``(workspace_id, stream_id)`` (SINK-7)."""
    out: list[dict[str, Any]] = []
    for i in range(n):
        env = dict(make_internal_event(seq_offset=i))
        env["workspace_id"] = workspace_id
        env["stream_id"] = stream_id
        out.append(env)
    return out


def _deliver(workspace_id: str, stream_id: str, events: list[dict[str, Any]]) -> None:
    from tenancy.application.services import worker_workspace_scope

    batch = make_batch(events, workspace_id=workspace_id, stream_id=stream_id)  # type: ignore[arg-type]
    with worker_workspace_scope(uuid.UUID(workspace_id)):
        result = BufferWriterChannel().deliver(batch)
    assert result.status == "ok", result.error


def test_copy_persists_delivered_shape(buffer_ws: Any) -> None:
    """The COPY path writes the delivered 20-key shape into ``event_buffer`` (BW-2/5)."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 5))

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [ws_id])
        cursor.execute(
            "SELECT buffer_seq, envelope FROM event_buffer WHERE stream_id = %s "
            "ORDER BY partition_ts, buffer_seq",
            [stream_id],
        )
        rows = cursor.fetchall()
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]  # per-stream monotonic (BW-6)
    for _seq, env in rows:
        parsed = json.loads(env) if isinstance(env, str) else env
        assert len(parsed) == 20  # delivered shape exactly (BW-5)
        assert not any(str(k).startswith("_df") for k in parsed)  # SB-2/SB-3


def test_buffer_seq_recovers_across_channels(buffer_ws: Any) -> None:
    """``buffer_seq`` continues gaplessly after a cold-start channel (BW-8) over PG."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 3))
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 4))  # fresh channel each call

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [ws_id])
        cursor.execute(
            "SELECT buffer_seq FROM event_buffer WHERE stream_id = %s ORDER BY buffer_seq",
            [stream_id],
        )
        seqs = [r[0] for r in cursor.fetchall()]
    assert seqs == [1, 2, 3, 4, 5, 6, 7]


def test_row_routes_to_partition_ts_partition(buffer_ws: Any) -> None:
    """A written row lands in the hourly partition matching its ``partition_ts`` (§4.3)."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())
    # Ensure the current hour's partition exists (the migration pre-creates a window).
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with connection.cursor() as cursor:
        partitions.ensure_partitions(cursor, start=now, hours_ahead=1)
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 1))

    part = partitions.partition_name(now)
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [ws_id])
        cursor.execute(f"SELECT count(*) FROM {part} WHERE stream_id = %s", [stream_id])
        assert cursor.fetchone()[0] == 1


def test_foreign_guc_sees_zero_rows(buffer_ws: Any) -> None:
    """Under a foreign workspace GUC, raw SELECTs over ``event_buffer`` return 0 (RLS)."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 2))

    foreign = str(uuid.uuid4())
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [foreign])
        cursor.execute(
            "SELECT count(*) FROM event_buffer WHERE workspace_id = %s", [ws_id]
        )
        assert cursor.fetchone()[0] == 0


def test_unset_guc_sees_zero_rows(buffer_ws: Any) -> None:
    """With the GUC unset, raw SELECTs over ``event_buffer`` return 0 (default-deny)."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())  # the batch + its envelopes must share a stream (SINK-7)
    _deliver(ws_id, stream_id, _events_for(ws_id, stream_id, 1))
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', '', true)")
        cursor.execute("SELECT count(*) FROM event_buffer")
        assert cursor.fetchone()[0] == 0


def test_chaos_duplicate_distinct_offsets_both_stored(buffer_ws: Any) -> None:
    """Chaos duplicates (same event_id, distinct offsets) are both stored (SINK-4/BW-4)."""
    _skip_unless_postgres()
    ws_id = str(buffer_ws.id)
    stream_id = str(uuid.uuid4())
    base = dict(order_placed_envelope(seed=4242))
    base["workspace_id"] = ws_id
    base["stream_id"] = stream_id
    _deliver(ws_id, stream_id, [base, dict(base)])  # identical event_id

    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [ws_id])
        cursor.execute(
            "SELECT count(*) FROM event_buffer WHERE stream_id = %s", [stream_id]
        )
        assert cursor.fetchone()[0] == 2  # NOT deduped on event_id (BW-4)
