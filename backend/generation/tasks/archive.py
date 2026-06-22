"""Ledger archive-to-Parquet beat task (P11-11; deployment-architecture §9.2-9.3).

``generation.archive_ledger_partitions`` runs daily at 02:00 UTC on the
``maintenance`` queue. It exports every ground-truth-ledger partition older than
the 48 h hot window to Parquet on object storage, **verifies the exported row count
matches the partition**, then drops the partition (database-schema §5.5; ADR-0017
keeps the hot 48 h synchronous, older ground truth served as async export). The
verify-before-drop ordering guarantees zero canonical loss within retention.

The DDL + the cross-tenant partition read both run on the owner-role connection
(the ``maintenance`` alias, falling back to ``default`` in the test lane) — the same
connection the partition-maintenance tasks already use for create/drop DDL (§7.1).
The task is idempotent: an already-archived (now-empty/absent) day is a no-op, so a
re-delivery (``task_acks_late``) re-runs harmlessly.

A failed archive (export mismatch, storage unreachable) does **not** drop the
partition and re-raises so the failure is recorded and the operational alert fires
(``BufferRetentionStalled`` covers stalled retention generally; the daily re-run
re-attempts). The metrics ``df_buffer_partitions_dropped_total`` /
``df_buffer_oldest_partition_age_seconds`` are owned by the buffer-retention path;
this task logs structured archive outcomes for the SLO/alert pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from celery import shared_task
from django.conf import settings
from django.db import connection, connections

logger = structlog.get_logger(__name__)

__all__ = ["archive_ledger_partitions"]

_MAINTENANCE_ALIAS = "maintenance"


def _ddl_connection() -> Any:
    """The owner-role connection for partition DDL/reads, or ``default``."""
    if _MAINTENANCE_ALIAS in connections:
        return connections[_MAINTENANCE_ALIAS]
    return connection


@shared_task(name="generation.archive_ledger_partitions", queue="maintenance")
def archive_ledger_partitions(*, now: datetime | None = None) -> dict[str, Any]:
    """Daily 02:00: archive ledger partitions older than the 48 h hot window, drop.

    Returns a per-day summary for the task log; a no-op on non-PostgreSQL (Parquet
    archival of RANGE partitions is a Postgres construct, mirroring partition_maint).
    """
    ddl = _ddl_connection()
    if ddl.vendor != "postgresql":
        logger.info("ledger.archive.skipped", reason="non-postgres vendor")
        return {"archived": [], "dropped": [], "rows": 0}

    from generation.infra import ledger_archive, partitions

    moment = now or datetime.now(UTC)
    hot_hours = int(settings.DF_LEDGER_HOT_HOURS)
    archive_dir = str(settings.DF_LEDGER_ARCHIVE_DIR)
    # The oldest day still inside total ledger retention; nothing past it should
    # exist (enforce_ledger_retention drops cold Parquet past the window).
    retention_days = int(settings.DF_LEDGER_RETENTION_DAYS)

    # Days strictly older than the hot window are archive-eligible. Scan a bounded
    # lookback below the horizon (covers a delayed/missed run); each day is idempotent.
    horizon_day = (moment - timedelta(hours=hot_hours)).date()
    candidate_days = [horizon_day - timedelta(days=i) for i in range(0, retention_days + 1)]

    archived: list[str] = []
    dropped: list[str] = []
    total_rows = 0
    with ddl.cursor() as cursor:
        for day in candidate_days:
            name = partitions.partition_name(day)
            # Skip days whose partition is not attached (already archived/dropped).
            cursor.execute(
                "SELECT to_regclass(%s) IS NOT NULL", [name]
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                continue
            result = ledger_archive.archive_partition(cursor, day, archive_dir=archive_dir)
            archived.append(result.partition)
            total_rows += result.row_count
            if result.dropped:
                dropped.append(result.partition)

    logger.info(
        "ledger.archive.task.done",
        archived=len(archived),
        dropped=len(dropped),
        rows=total_rows,
    )
    return {"archived": archived, "dropped": dropped, "rows": total_rows}
