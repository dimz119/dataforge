"""Partition-maintenance beat tasks (backend-architecture §7.1 maintenance queue).

Beat-scheduled time-based housekeeping (ADR-0013 retention) on the ``maintenance``
queue — control plane only, never generating (ADR-0006). Two cadences:

* ``streams.maintain_buffer_partitions`` — hourly: pre-create the next event_buffer
  hourly partitions and drop those past the 24 h retention (database-schema §6.1).
* ``streams.maintain_ledger_partitions`` — daily: ensure today + N ahead ledger
  daily partitions and drop those past the 7-day retention (database-schema §5.5).

Both delegate to :mod:`streams.infra.partition_maint`, which reuses the per-app
partition managers (generation for the ledger; delivery for the buffer). The DDL
needs the ``dataforge_migrate`` (owner) role; the partition-maint helpers run raw
DDL through the connection the task layer repoints (§7.1 "uses dataforge_migrate
role where partitions require it"). Tasks are idempotent (``IF NOT EXISTS`` /
``IF EXISTS``), so re-running is safe (``task_acks_late``).
"""

from __future__ import annotations

import structlog
from celery import shared_task

from streams.infra import partition_maint

logger = structlog.get_logger(__name__)

__all__ = ["maintain_buffer_partitions", "maintain_ledger_partitions"]


@shared_task(name="streams.maintain_ledger_partitions", queue="maintenance")
def maintain_ledger_partitions() -> dict[str, list[str]]:
    """Daily beat: ensure + retire ground_truth_ledger daily partitions (§5.5)."""
    result = partition_maint.maintain_ledger_partitions()
    logger.info(
        "maintain_ledger_partitions_task_done",
        created=len(result["created"]),
        dropped=len(result["dropped"]),
    )
    return result


@shared_task(name="streams.maintain_buffer_partitions", queue="maintenance")
def maintain_buffer_partitions() -> dict[str, list[str]]:
    """Hourly beat: ensure + retire event_buffer hourly partitions (§6.1)."""
    result = partition_maint.maintain_buffer_partitions()
    logger.info(
        "maintain_buffer_partitions_task_done",
        created=len(result["created"]),
        dropped=len(result["dropped"]),
    )
    return result
