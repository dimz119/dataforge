"""Partition-maintenance orchestration for the control plane (backend-architecture §7.1).

The ``maintenance`` queue's beat-scheduled housekeeping (ADR-0013 retention). This
is **control plane only** — it creates/drops time partitions, it never generates.
Two cadences:

* **event_buffer hourly** — the REST delivery buffer is hourly RANGE-partitioned
  (database-schema §6.1) with a 24 h retention; the hourly beat pre-creates the
  next hours and drops partitions past retention. The buffer partition manager is
  owned by the *delivery* app (buffer-writer area); this orchestrator calls its
  seam, gracefully skipping when that seam is not yet built (so the beat is safe to
  run before delivery lands).
* **ledger daily** — the ground-truth ledger is daily RANGE-partitioned
  (database-schema §5.5) with a 7-day retention; the daily beat ensures today + N
  ahead and drops partitions past retention. The ledger partition manager is owned
  by the *generation* app (``generation.infra.partitions``), reused here.

DDL runs as the ``dataforge_migrate`` (owner) role where partitions require it
(§7.1); the Celery task layer repoints the connection. This module is the pure
orchestration seam over the per-app partition managers (no role logic here).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from django.db import connection, connections

logger = structlog.get_logger(__name__)

# §8.1 "pre-created ahead" + retention (database-schema §5.5 / §6.1; ADR-0013).
_LEDGER_DAYS_AHEAD = 3
_LEDGER_RETENTION_DAYS = 7
_BUFFER_HOURS_AHEAD = 3
_BUFFER_RETENTION_HOURS = 24

# Partition DDL needs the owner (``dataforge_migrate``) role; the runtime
# ``default`` connection is NOBYPASSRLS and lacks DDL privileges by design
# (SEC-TEN-2). Settings registers the ``maintenance`` alias (owner role) for
# exactly this; when it is absent (the test lane connects as the owner via
# ``default``) we transparently fall back to ``default`` (§7.1).
_MAINTENANCE_ALIAS = "maintenance"

__all__ = ["maintain_buffer_partitions", "maintain_ledger_partitions"]


def _ddl_connection() -> Any:
    """The owner-role connection for partition DDL, or ``default`` if unconfigured."""
    if _MAINTENANCE_ALIAS in connections:
        return connections[_MAINTENANCE_ALIAS]
    return connection


def maintain_ledger_partitions(*, days_ahead: int = _LEDGER_DAYS_AHEAD) -> dict[str, list[str]]:
    """Daily: ensure today + ``days_ahead`` ledger partitions, drop past retention.

    Reuses ``generation.infra.partitions`` (the ledger partition manager, M-5). A
    no-op on non-PostgreSQL (partitioning is a Postgres construct). Returns the
    created + dropped partition names for the task log.
    """
    ddl = _ddl_connection()
    if ddl.vendor != "postgresql":
        logger.info("ledger_partition_maint_skipped", reason="non-postgres vendor")
        return {"created": [], "dropped": []}
    from generation.infra import partitions

    today = datetime.now(UTC).date()
    created: list[str] = []
    dropped: list[str] = []
    with ddl.cursor() as cursor:
        created = partitions.ensure_partitions(cursor, start=today, days_ahead=days_ahead)
        # Drop the day that has just fallen out of the 7-day window (idempotent).
        expired_day = today - timedelta(days=_LEDGER_RETENTION_DAYS + 1)
        dropped.append(partitions.drop_partition(cursor, expired_day))
    logger.info(
        "ledger_partition_maint_done", created=len(created), dropped=len(dropped)
    )
    return {"created": created, "dropped": dropped}


def maintain_buffer_partitions(
    *, hours_ahead: int = _BUFFER_HOURS_AHEAD
) -> dict[str, list[str]]:
    """Hourly: ensure the next ``hours_ahead`` buffer partitions, drop past 24 h.

    The buffer partition manager is owned by the delivery app (buffer-writer area).
    This orchestrator calls its seam and gracefully skips when it is not yet built
    (the beat is safe to run before delivery lands). A no-op on non-PostgreSQL.
    """
    ddl = _ddl_connection()
    if ddl.vendor != "postgresql":
        logger.info("buffer_partition_maint_skipped", reason="non-postgres vendor")
        return {"created": [], "dropped": []}
    buffer_partitions = _load_buffer_partition_manager()
    if buffer_partitions is None:
        logger.info("buffer_partition_maint_skipped", reason="delivery seam not yet built")
        return {"created": [], "dropped": []}
    now = datetime.now(UTC)
    created: list[str] = []
    dropped: list[str] = []
    with ddl.cursor() as cursor:
        created = list(
            buffer_partitions.ensure_partitions(cursor, start=now, hours_ahead=hours_ahead)
        )
        expired_hour = now - timedelta(hours=_BUFFER_RETENTION_HOURS + 1)
        dropped.append(buffer_partitions.drop_partition(cursor, expired_hour))
    logger.info(
        "buffer_partition_maint_done", created=len(created), dropped=len(dropped)
    )
    return {"created": created, "dropped": dropped}


def _load_buffer_partition_manager() -> Any | None:
    """Lazily import the delivery buffer partition manager, or None if not built yet.

    The delivery app ships the hourly ``event_buffer`` partition DDL as
    ``delivery.infra.partitions`` (``ensure_partitions(cursor, start, hours_ahead)``
    + ``drop_partition(cursor, hour)``), which the migration already uses to seed the
    create-ahead window. The maintenance beat keeps that window rolling each hour.
    """
    try:
        from delivery.infra import partitions as buffer_partitions
    except ImportError:
        return None
    return buffer_partitions
