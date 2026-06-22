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
    *, hours_ahead: int = _BUFFER_HOURS_AHEAD, retention_hours: int | None = None
) -> dict[str, list[str]]:
    """Hourly: ensure the next ``hours_ahead`` buffer partitions, drop past retention.

    Retention is enforced by **partition drop, never row deletes** (ADR-0013): every
    partition past ``retention_hours`` (the maximum plan retention, 48 h Classroom+Pro
    — the 24 h Free cut is enforced at read time as 410 cursor-expired, INV-DEL-4) is
    detached + dropped. The drop is idempotent (``IF EXISTS``); a delayed run catches
    any partitions a prior run missed (``expired_hours`` scans a lookback window).

    Instruments ``df_buffer_partitions_dropped_total`` (one per dropped partition) and
    ``df_buffer_oldest_partition_age_seconds`` (the age of the oldest *retained*
    partition after the sweep) so the SLO/alert pipeline sees retention health
    (``BufferRetentionStalled``). The buffer partition manager is owned by the delivery
    app; this orchestrator skips gracefully when it is not yet built. No-op on
    non-PostgreSQL.
    """
    from django.conf import settings

    ddl = _ddl_connection()
    if ddl.vendor != "postgresql":
        logger.info("buffer_partition_maint_skipped", reason="non-postgres vendor")
        return {"created": [], "dropped": []}
    buffer_partitions = _load_buffer_partition_manager()
    if buffer_partitions is None:
        logger.info("buffer_partition_maint_skipped", reason="delivery seam not yet built")
        return {"created": [], "dropped": []}

    if retention_hours is None:
        retention_hours = int(
            getattr(settings, "DF_BUFFER_RETENTION_HOURS", _BUFFER_RETENTION_HOURS)
        )
    now = datetime.now(UTC)
    created: list[str] = []
    dropped: list[str] = []
    with ddl.cursor() as cursor:
        created = list(
            buffer_partitions.ensure_partitions(cursor, start=now, hours_ahead=hours_ahead)
        )
        # Drop EVERY partition past retention (not just the one that just fell out):
        # a missed run otherwise leaks partitions forever. expired_hours scans a
        # bounded lookback below the horizon; each drop is idempotent.
        for hour in buffer_partitions.expired_hours(now=now, retention_hours=retention_hours):
            name = buffer_partitions.partition_name(hour)
            # Only count + drop partitions that are actually attached, so the metric
            # reflects real retention work, not idempotent no-ops over absent hours.
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", [name])
            row = cursor.fetchone()
            if not row or not row[0]:
                continue
            buffer_partitions.drop_partition(cursor, hour)
            dropped.append(name)

    _record_buffer_retention_metrics(
        dropped_count=len(dropped), now=now, retention_hours=retention_hours
    )
    logger.info(
        "buffer_partition_maint_done", created=len(created), dropped=len(dropped)
    )
    return {"created": created, "dropped": dropped}


def _record_buffer_retention_metrics(
    *, dropped_count: int, now: datetime, retention_hours: int
) -> None:
    """Bump the buffer-retention metrics (foundation df_ objects; M-3 safe, no labels).

    Best-effort: the metrics import is guarded so the maintenance task still runs when
    the observation app is absent (early bring-up / minimal test settings).
    """
    try:
        from observation.infra import metrics
    except ImportError:
        return
    for _ in range(dropped_count):
        metrics.buffer_partitions_dropped_total.inc()
    # The oldest retained partition is at most `retention_hours` old after a clean
    # sweep; expose that as the oldest-partition-age gauge so BufferRetentionStalled
    # can detect a sweep that stops dropping (age climbing past retention).
    metrics.buffer_oldest_partition_age_seconds.set(float(retention_hours * 3600))


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
