"""Celery task entrypoints for the Generation context — the exports-queue batch
job (backend-architecture §3.1, §7.1; api-spec §4.10).

A large dataset (estimate > the sync threshold) is generated asynchronously on the
``exports`` queue: the task drives the engine in backfill/unpaced mode → ledger →
gzipped JSONL artifact, flipping the :class:`Dataset` row through
``generating`` → ``ready`` (or ``failed``). The task is a thin transport calling
one application service (``generate_dataset``); it is idempotent (acks-late + the
idempotent ledger append + artifact overwrite) so a re-delivery re-derives the
identical dataset (INV-G-4).
"""

from __future__ import annotations

import uuid

import structlog
from celery import shared_task

# Re-export the data-lifecycle beat tasks (P11-11) so Celery's
# ``autodiscover_tasks(related_name="tasks")`` registers them when the
# ``generation.tasks`` package is imported.
from generation.tasks.archive import archive_ledger_partitions

logger = structlog.get_logger(__name__)

__all__ = [
    "archive_ledger_partitions",
    "enqueue_dataset_generation",
    "generate_dataset_task",
]


@shared_task(
    name="generation.generate_dataset",
    queue="exports",
    acks_late=True,
    bind=False,
)
def generate_dataset_task(dataset_id: str, workspace_id: str) -> None:
    """Generate a queued dataset on the exports queue (api-spec §4.10 async path).

    Arms the workspace context for the duration so the ledger/snapshot/checkpoint
    writes run with RLS applied (the runtime ``dataforge_app`` role), then delegates
    to the application service.
    """
    from generation.application import services
    from tenancy.application.services import worker_workspace_scope

    ws_uuid = uuid.UUID(workspace_id)
    try:
        # Arm both RLS layers (contextvar + the Postgres GUC) inside a transaction so
        # the ledger/snapshot/checkpoint writes (and the dataset-row reads/updates)
        # run with RLS applied under the NOBYPASSRLS runtime role; a bare contextvar
        # would leave app_workspace_id() NULL and the row would be invisible.
        with worker_workspace_scope(ws_uuid):
            services.generate_dataset(uuid.UUID(dataset_id), workspace_id=workspace_id)
    except services.DatasetGenerationError as exc:
        # The generation transaction rolled back (the failure marker with it); record
        # the failure in a fresh armed transaction so polling surfaces it (the task
        # does not retry a deterministic generation error).
        logger.error("dataset_generation_failed", dataset_id=dataset_id, error=str(exc))
        try:
            with worker_workspace_scope(ws_uuid):
                services.mark_dataset_failed(
                    uuid.UUID(dataset_id), workspace_id=workspace_id, reason=str(exc)
                )
        except Exception:  # best-effort failure recording
            logger.error("dataset_failure_record_failed", dataset_id=dataset_id)


def enqueue_dataset_generation(dataset_id: str, workspace_id: str) -> None:
    """Hand a dataset off to the exports queue (the create command's async path)."""
    generate_dataset_task.delay(dataset_id, workspace_id)
