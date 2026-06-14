"""Celery task entrypoints for the Scenario Catalog context — thin transports
calling one application service each (backend-architecture §3.1).

The Layer-3 dry-run job (plugin-arch §8.4) rides the ``validation`` queue
(backend-architecture §7.1): after L1+L2 pass synchronously on ingest/publish, L3
runs the **real generic runtime** in the §8.4 sandbox as this async job, persists
the merged §8.3 report onto the ManifestVersion, and the report is polled via the
existing ``…/versions/{v}/validation`` endpoint. The task is a thin transport over
``catalog.application.validation_l3.run_layer3_for_version``; it is idempotent
(re-running L3 on the same immutable document re-derives the same ``dry_run``
content — the fixed sandbox seed, §8.4) so an acks-late re-delivery is safe.
"""

from __future__ import annotations

import uuid

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)

__all__ = ["enqueue_layer3_dry_run", "validate_manifest_l3_task"]


@shared_task(
    name="catalog.validate_manifest_l3",
    queue="validation",
    acks_late=True,
    bind=False,
)
def validate_manifest_l3_task(definition_id: str, workspace_id: str | None) -> None:
    """Run the Layer-3 dry run for a ManifestVersion on the ``validation`` queue.

    ``workspace_id`` is the owning workspace for a tenant (``workspace``-visibility)
    manifest, ``None`` for a global builtin. A tenant run arms the workspace context
    so the ``validation_report`` write runs under RLS (the runtime ``dataforge_app``
    role); a global run needs no context (the row carries a NULL workspace, written
    by the maintenance role). The dry run itself touches no tenant data (it executes
    against a throwaway in-memory ledger, §8.4) — the context covers only the report
    persistence.
    """
    from catalog.application import validation_l3
    from tenancy.application.services import worker_workspace_scope

    ws_uuid = uuid.UUID(workspace_id) if workspace_id is not None else None
    try:
        # Arm both RLS layers (contextvar + the Postgres GUC) inside a transaction so
        # the NOBYPASSRLS runtime role can see the tenant draft row; a bare contextvar
        # would leave app_workspace_id() NULL and RLS would hide the row.
        with worker_workspace_scope(ws_uuid):
            outcome = validation_l3.run_layer3_for_version(uuid.UUID(definition_id))
        logger.info(
            "manifest_l3_dry_run_complete",
            definition_id=definition_id,
            ran=outcome.ran,
            passed=outcome.passed,
            est_eps_per_shard=outcome.est_eps_per_shard,
            dry_run_codes=outcome.dry_run_codes,
        )
    except validation_l3.ManifestVersionMissing:
        # The row was deleted between enqueue and execution; nothing to validate.
        logger.warning("manifest_l3_target_missing", definition_id=definition_id)


def enqueue_layer3_dry_run(definition_id: str, workspace_id: str | None) -> None:
    """Hand a ManifestVersion off to the ``validation`` queue for the L3 dry run.

    Called after L1+L2 pass on ingest/publish; the report is then polled via the
    existing validation-report endpoint (§8.4).
    """
    validate_manifest_l3_task.delay(definition_id, workspace_id)
