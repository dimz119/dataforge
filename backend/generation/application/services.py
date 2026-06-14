"""Use-case services for the Generation context — dataset (backfill batch)
orchestration (api-spec §4.10; behavior-engine §8; PRD §7).

The service owns the transaction boundary and orchestrates: resolve the pinned
scenario instance → build the merged manifest + the :class:`BatchPlan` → enforce
the PRD §7 backfill quota caps at command time → decide sync-small vs async-large
(the 50,000-event boundary) → persist the :class:`Dataset` row + audit event →
run the engine (synchronously now, or hand off to the exports-queue Celery task).

Cross-app references stay in the application/domain layers (the import contract):
the pinned manifest version is read through ``catalog.application``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction

from dataforge_engine.seeds import SEED_MAX, SEED_MIN
from generation.application import engine_driver
from generation.domain.models import (
    DATASET_FAILED,
    DATASET_GENERATING,
    DATASET_QUEUED,
    DATASET_READY,
    Dataset,
)
from generation.infra import quotas, storage

if TYPE_CHECKING:
    from generation.application.engine_driver import BatchPlan

__all__ = [
    "DatasetGenerationError",
    "InstanceNotFound",
    "QuotaExceeded",
    "create_dataset",
    "generate_dataset",
    "get_dataset",
    "list_datasets",
    "mark_dataset_failed",
]


class InstanceNotFound(Exception):
    """The pinned scenario instance is absent in the active workspace (→ 404)."""


class QuotaExceeded(Exception):
    """A backfill cap was breached at command time (→ 403 quota-exceeded)."""

    def __init__(self, *, quota: str, limit: int, requested: int) -> None:
        super().__init__(f"{quota} {requested} exceeds {limit}")
        self.quota = quota
        self.limit = limit
        self.requested = requested


class DatasetGenerationError(Exception):
    """Generation failed after the dataset row was created (status → failed)."""


@dataclass
class CreateResult:
    """The outcome of a create command: the row + whether it ran synchronously."""

    dataset: Dataset
    sync: bool


def list_datasets(*, workspace: Any, status: str | None = None) -> list[Dataset]:
    """The workspace's datasets, newest first (Class-T scoped manager)."""
    qs = Dataset.objects.filter(workspace=workspace)
    if status:
        qs = qs.filter(status=status)
    return list(qs.order_by("-created_at"))


def get_dataset(dataset_id: uuid.UUID, *, workspace: Any) -> Dataset | None:
    """One dataset by id within the active workspace (foreign id → None → 404)."""
    return Dataset.objects.filter(id=dataset_id, workspace=workspace).first()


def _resolve_instance(scenario_instance_id: uuid.UUID, *, workspace: Any) -> Any:
    from catalog.application import services as catalog_services

    instance = catalog_services.get_instance(scenario_instance_id, workspace=workspace)
    if instance is None:
        raise InstanceNotFound()
    return instance


def _plan_for(
    *, instance: Any, workspace_id: str, stream_id: str, seed: int,
    simulated_days: int, virtual_epoch: datetime,
) -> BatchPlan:
    from generation.application.engine_driver import (
        BatchPlan,
        canonical_sha256,
        dry_run_rates,
        merged_document_for,
    )

    definition = instance.scenario_definition
    manifest = definition.manifest
    overlay = instance.overrides or {}
    merged = merged_document_for(manifest, overlay)
    config_sha = canonical_sha256(merged)
    pin_sha = canonical_sha256(
        {
            "manifest_version": definition.version,
            "config_revision": instance.config_version,
            "manifest_sha256": definition.manifest_sha256,
        }
    )
    schema_versions = _schema_versions_for(definition)
    mes, vpd = dry_run_rates(definition.validation_report)
    return BatchPlan(
        workspace_id=workspace_id,
        stream_id=stream_id,
        seed=seed,
        merged_document=merged,
        pin_sha256=pin_sha,
        config_sha256=config_sha,
        schema_versions=schema_versions,
        virtual_epoch=virtual_epoch,
        simulated_days=simulated_days,
        mean_events_per_session=mes,
        visits_per_actor_day=vpd,
    )


def _schema_versions_for(definition: Any) -> dict[str, int]:
    """Effective schema versions for the pinned manifest (registry seam).

    The pinned manifest registers v1 schemas at publish (registry derivation); a
    batch stamps every ``schema_ref`` at v1. The registry reader seam supplies
    later versions when schema upgrades exist (Phase 10); absent it, v1 is correct.
    """
    return {}


def create_dataset(
    *,
    workspace: Any,
    scenario_instance_id: uuid.UUID,
    name: str,
    seed: int | None,
    simulated_days: int,
    virtual_epoch: datetime | None,
    compression: str,
    actor: Any,
) -> CreateResult:
    """Create + (sync-small) run or (async-large) queue a backfill dataset.

    Enforces the PRD §7 backfill caps at command time (before any row is written),
    persists the :class:`Dataset` row with the pin echo + audit event, and either
    runs the engine synchronously (estimate ≤ threshold) or hands off to the
    exports-queue Celery task (202).
    """
    instance = _resolve_instance(scenario_instance_id, workspace=workspace)
    workspace_id = str(workspace.id)
    resolved_seed = _resolve_seed(seed, instance)
    epoch = _resolve_epoch(virtual_epoch, simulated_days)
    # Deterministic stream_id over the determinism tuple so a same-input batch is
    # byte-identical on regeneration (INV-G-4); persisted on the row and re-read by
    # the async path's _rebuild_plan.
    stream_id = _derive_stream_id(
        instance_id=scenario_instance_id,
        seed=resolved_seed,
        simulated_days=simulated_days,
        virtual_epoch=epoch,
    )

    plan = _plan_for(
        instance=instance, workspace_id=workspace_id, stream_id=stream_id,
        seed=resolved_seed, simulated_days=simulated_days, virtual_epoch=epoch,
    )

    caps = quotas.backfill_caps_for(workspace_id)
    estimated = engine_driver.estimate_events(plan)
    try:
        quotas.enforce_backfill(
            caps=caps, simulated_days=simulated_days, estimated_events=estimated
        )
    except quotas.QuotaExceededError as exc:
        raise QuotaExceeded(quota=exc.quota, limit=exc.limit, requested=exc.requested) from exc

    threshold = int(getattr(settings, "DATASET_SYNC_EVENT_THRESHOLD", 50_000))
    sync = estimated <= threshold

    with transaction.atomic():
        dataset = Dataset.objects.create(
            workspace=workspace,
            scenario_instance_id=scenario_instance_id,
            name=name,
            status=DATASET_QUEUED,
            seed=resolved_seed,
            stream_id=stream_id,
            pin_sha256=plan.pin_sha256,
            simulated_from=plan.virtual_epoch,
            simulated_to=plan.simulated_to,
            estimated_events=estimated,
            compression=compression,
            created_by=getattr(actor, "id", None),
        )
        _audit_created(dataset, actor=actor, scenario_instance_id=scenario_instance_id, sync=sync)

    if sync:
        generate_dataset(dataset.id, workspace_id=workspace_id, plan=plan)
        dataset.refresh_from_db()
    # The async (large) path is enqueued by the caller (api/tasks layer) — the
    # application layer must not import generation.tasks (the app-layering import
    # contract). The viewset reads ``CreateResult.sync`` and hands off to the
    # exports queue when ``sync`` is False.
    return CreateResult(dataset=dataset, sync=sync)


def generate_dataset(
    dataset_id: uuid.UUID, *, workspace_id: str, plan: BatchPlan | None = None
) -> None:
    """Run the engine for a dataset → ledger → JSONL artifact (sync + task body).

    Idempotent and re-runnable (Celery acks-late): the ledger append is idempotent
    on ``(stream, shard, sequence_no)`` and the artifact is overwritten. On failure
    the row is marked ``failed`` with a reason; the exception is re-raised so the
    task records the failure.
    """
    # tenancy: unscoped — task path may run without a request context; workspace_id
    # is verified against the row below and RLS still applies at the DB.
    dataset = Dataset.all_objects.filter(id=dataset_id).first()
    if dataset is None:
        raise DatasetGenerationError(f"dataset {dataset_id} not found")
    if str(dataset.workspace_id) != workspace_id:
        raise DatasetGenerationError("workspace mismatch")
    if plan is None:
        plan = _rebuild_plan(dataset, workspace_id=workspace_id)

    dataset.status = DATASET_GENERATING
    dataset.progress = 0.0
    dataset.save(update_fields=["status", "progress"])

    try:
        # Backfill emitted_at is a wall field that "carries no business meaning" and
        # is "near-constant" (event-model §164). A DeterministicWallClock anchored at
        # the start of the current UTC day makes the full delivered envelope (wall
        # fields included) byte-identical for same-day regeneration (INV-G-4 / the
        # GOLD harness, testing-strategy §6) while keeping emitted_at inside the
        # ledger's pre-created daily partition (the ledger is PARTITION BY RANGE
        # (emitted_at); the runtime role has no DDL grant to create partitions).
        from generation.infra.clock import DeterministicWallClock

        wall_anchor = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        clock = DeterministicWallClock(epoch=wall_anchor)
        produced = engine_driver.run_batch(plan, clock=clock)
        path, count, size = storage.write_jsonl(
            dataset_id=str(dataset.id), envelopes=produced, compression=dataset.compression
        )
    except Exception as exc:
        dataset.status = DATASET_FAILED
        dataset.failure_reason = str(exc)[:500]
        dataset.save(update_fields=["status", "failure_reason"])
        raise DatasetGenerationError(str(exc)) from exc

    now = datetime.now(UTC)
    dataset.status = DATASET_READY
    dataset.progress = 1.0
    dataset.event_count = count
    dataset.size_bytes = size
    dataset.file_path = str(path)
    dataset.ready_at = now
    dataset.expires_at = now + timedelta(days=7)
    dataset.save(
        update_fields=[
            "status", "progress", "event_count", "size_bytes",
            "file_path", "ready_at", "expires_at",
        ]
    )


def mark_dataset_failed(
    dataset_id: uuid.UUID, *, workspace_id: str, reason: str
) -> None:
    """Record a generation failure on the row (async task path, own transaction).

    The async task runs generation in a GUC-armed transaction; on failure that
    transaction (and the in-band failure marker) rolls back, so the task calls this
    in a fresh armed transaction to persist ``status=failed`` + ``failure_reason``
    so polling surfaces it. Idempotent.
    """
    # tenancy: unscoped — task path; workspace_id verified below, RLS applies at DB.
    dataset = Dataset.all_objects.filter(id=dataset_id).first()
    if dataset is None or str(dataset.workspace_id) != workspace_id:
        return
    dataset.status = DATASET_FAILED
    dataset.failure_reason = reason[:500]
    dataset.save(update_fields=["status", "failure_reason"])


def _rebuild_plan(dataset: Dataset, *, workspace_id: str) -> BatchPlan:
    """Rebuild the deterministic BatchPlan from a persisted dataset row.

    The async task re-derives the plan from the pinned instance + the dataset's
    pin echo (seed, window) so generation is identical to the would-be sync run.
    """
    from catalog.domain.models import ScenarioInstance

    # tenancy: unscoped — task path; workspace_id checked by caller, RLS applies at DB.
    instance = ScenarioInstance.all_objects.filter(
        id=dataset.scenario_instance_id, workspace_id=workspace_id
    ).first()
    if instance is None:
        raise DatasetGenerationError("scenario instance gone")
    simulated_days = (dataset.simulated_to - dataset.simulated_from).days
    return _plan_for(
        instance=instance, workspace_id=workspace_id, stream_id=str(dataset.stream_id),
        seed=dataset.seed, simulated_days=simulated_days, virtual_epoch=dataset.simulated_from,
    )


def _resolve_seed(seed: int | None, instance: Any) -> int:
    """The batch seed: explicit > instance default > server-generated (R-3 domain)."""
    if seed is not None:
        return seed
    if getattr(instance, "default_seed", None) is not None:
        return int(instance.default_seed)
    import secrets

    return SEED_MIN + secrets.randbelow(SEED_MAX - SEED_MIN + 1)


# A fixed, UTC-aligned anchor for the default backfill window. A backfill's
# emitted_at carries no business meaning (event-model §161-164) and occurred_at is
# never derived from wall time (§157); anchoring the *default* virtual_epoch to a
# fixed instant (rather than wall ``now()``) makes a same-(seed, instance, window)
# dataset byte-identical on regeneration — the INV-G-4 guarantee the API spec
# states ("regenerate with the same seed for an identical dataset"). An explicit
# ``virtual_epoch`` in the request still overrides this default.
_DEFAULT_EPOCH_ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)


def _resolve_epoch(virtual_epoch: datetime | None, simulated_days: int) -> datetime:
    """The batch virtual_epoch: explicit > a deterministic fixed anchor.

    api §4.10.1 documents the default as "request time - simulated_days", but a
    wall-derived default makes two same-seed requests diverge (different
    occurred_at, different UUIDv7 time bits → not byte-identical), contradicting
    INV-G-4. The default is therefore a *deterministic* window ending at the fixed
    anchor; supplying ``virtual_epoch`` explicitly restores request-relative
    placement.
    """
    if virtual_epoch is not None:
        return virtual_epoch if virtual_epoch.tzinfo else virtual_epoch.replace(tzinfo=UTC)
    return _DEFAULT_EPOCH_ANCHOR - timedelta(days=simulated_days)


def _derive_stream_id(
    *, instance_id: uuid.UUID, seed: int, simulated_days: int, virtual_epoch: datetime
) -> str:
    """A deterministic stream_id for a logical batch (INV-G-4 byte-identity).

    The ``stream_id`` is stamped into every envelope (and the partition_key), so a
    random uuid4 per request would break same-input byte-identity. Derive a UUIDv5
    over the determinism tuple (instance pin + seed + window) so the same logical
    batch reproduces an identical stream_id, while distinct batches stay distinct.
    """
    basis = (
        f"{instance_id}:{seed}:{simulated_days}:{virtual_epoch.astimezone(UTC).isoformat()}"
    )
    digest = hashlib.sha256(basis.encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=5))


def _audit_created(
    dataset: Dataset, *, actor: Any, scenario_instance_id: uuid.UUID, sync: bool
) -> None:
    try:
        from audit.application.writer import record_audit
    except ImportError:
        return
    record_audit(
        action="generation.dataset.created",
        actor=actor,
        workspace_id=dataset.workspace_id,
        target={"type": "dataset", "id": str(dataset.id), "label": dataset.name},
        metadata={
            "scenario_instance_id": str(scenario_instance_id),
            "seed": dataset.seed,
            "estimated_events": dataset.estimated_events,
            "mode": "sync" if sync else "async",
        },
    )
