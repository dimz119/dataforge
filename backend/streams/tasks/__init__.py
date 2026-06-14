"""Celery task entrypoints for the Stream Control context — thin transports
calling one application service each (backend-architecture §3.1).

Control plane only (ADR-0006): lifecycle command handlers (system pause / fail),
the lease-expiry watchdog (T4/T11), and the partition-maintenance beat jobs. No
generation task exists here (the data plane is the runner). The submodule tasks are
re-exported so Celery's ``autodiscover_tasks(related_name="tasks")`` registers them
when the ``streams.tasks`` package is imported.
"""

from streams.tasks.lifecycle import fail_stream, system_pause_stream
from streams.tasks.maintenance import (
    maintain_buffer_partitions,
    maintain_ledger_partitions,
)
from streams.tasks.watchdog import lease_expiry_watchdog

__all__ = [
    "fail_stream",
    "lease_expiry_watchdog",
    "maintain_buffer_partitions",
    "maintain_ledger_partitions",
    "system_pause_stream",
]
