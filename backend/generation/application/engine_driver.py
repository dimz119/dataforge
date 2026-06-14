"""The engine-driving core for batch generation (behavior-engine §8; ADR-0008).

A thin, generic host that compiles the pinned merged manifest into an IR, builds a
backfill :class:`~dataforge_engine.behavior.ShardConfig`, and drives
:meth:`Shard.run_batch` over the injected ports (ledger sink, pool store, wall
clock). Zero scenario knowledge (BE-T1): everything flows from the manifest IR.

Used by both the synchronous dataset path (small batches) and the Celery exports
task (large batches). The same code runs under the deterministic golden wall clock
(GOLD-A) — the wall clock is injected, never read directly (BE-ENG-2).

The driver returns the produced envelopes (in ``(shard_id, sequence_no)`` order,
CDC ``r`` snapshots at the head) for the JSONL writer, and persists the checkpoint
codec blob + pool snapshots at finalization (the FORMAT ships now; lease-driven
pause/resume is Phase 5-6).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from dataforge_engine.behavior import (
    Shard,
    ShardConfig,
    compile_manifest_cached,
    encode_checkpoint,
)
from dataforge_engine.manifest import ManifestView, merge_overlay
from generation.infra.clock import SystemWallClock
from generation.infra.ledger_sink import LedgerSink
from generation.infra.snapshot_store import SnapshotSink, write_checkpoint

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.ports import WallClock

# Default behavioural rates when the manifest carries no dry-run estimate (the L3
# report lands separately this phase). Conservative; the real estimate overrides.
_DEFAULT_MEAN_EVENTS_PER_SESSION = 5.0
_DEFAULT_VISITS_PER_ACTOR_DAY = 1.0
_US_PER_DAY = 86_400 * 1_000_000


@dataclass(frozen=True)
class BatchPlan:
    """The resolved, pinned inputs for one backfill batch (immutable)."""

    workspace_id: str
    stream_id: str
    seed: int
    merged_document: dict[str, Any]
    pin_sha256: str
    config_sha256: str
    schema_versions: dict[str, int]
    virtual_epoch: datetime
    simulated_days: int
    mean_events_per_session: float
    visits_per_actor_day: float

    @property
    def until_us(self) -> int:
        """Simulated-window end in microseconds from the virtual epoch (BE-F3)."""
        return self.simulated_days * _US_PER_DAY

    @property
    def simulated_to(self) -> datetime:
        return self.virtual_epoch + timedelta(days=self.simulated_days)


def _json_default(value: Any) -> str:
    """Render a ``Decimal`` (remembered cart value, etc.) as its literal digits."""
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to the checkpoint blob")


def merged_document_for(manifest: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply the instance overlay to the pinned manifest (the IR's input, §11)."""
    return merge_overlay(manifest, overlay or {})


def canonical_sha256(document: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a document (the pin / config digest)."""
    canonical = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dry_run_rates(validation_report: dict[str, Any] | None) -> tuple[float, float]:
    """Pull (mean_events_per_session, visits_per_actor_day) from the L3 dry-run.

    Falls back to conservative defaults when the report has no ``dry_run`` member
    (the report is populated by the validator's L3 job; the two are independent).
    """
    report = validation_report or {}
    dry = report.get("dry_run") or {}
    mes = float(dry.get("mean_events_per_session", _DEFAULT_MEAN_EVENTS_PER_SESSION) or
                _DEFAULT_MEAN_EVENTS_PER_SESSION)
    vpd = float(dry.get("visits_per_actor_day", _DEFAULT_VISITS_PER_ACTOR_DAY) or
                _DEFAULT_VISITS_PER_ACTOR_DAY)
    return mes, vpd


def estimate_events(plan: BatchPlan) -> int:
    """Estimate total events for the batch (quota + sync/async decision).

    ``sessions ≈ actor_catalog_size · visits_per_actor_day · simulated_days``;
    ``events ≈ sessions · mean_events_per_session`` (+ the head snapshot rows). The
    estimate derives entirely from manifest data (the dry-run rates + seeding
    catalog sizes), so it is generic.
    """
    view = ManifestView(plan.merged_document)
    actor = view.actor_entity
    catalog_sizes = view.seeding.get("catalogs", {})
    actor_size = int((catalog_sizes.get(actor) or {}).get("default", 0))
    seeded_total = sum(int((c or {}).get("default", 0)) for c in catalog_sizes.values())
    sessions = actor_size * plan.visits_per_actor_day * plan.simulated_days
    return int(sessions * plan.mean_events_per_session) + seeded_total


def build_shard(plan: BatchPlan, clock: WallClock | None = None) -> Shard:
    """Compile the IR and build a backfill Shard for ``plan`` (generic)."""
    ir = compile_manifest_cached(
        plan.merged_document,
        config_sha256=plan.config_sha256,
        schema_versions=plan.schema_versions,
    )
    config = ShardConfig(
        seed=plan.seed,
        workspace_id=plan.workspace_id,
        stream_id=plan.stream_id,
        shard_id=0,
        virtual_epoch=plan.virtual_epoch,
        mode="backfill",
        mean_events_per_session=plan.mean_events_per_session,
        visits_per_actor_day=plan.visits_per_actor_day,
    )
    return Shard(ir, config, clock or SystemWallClock())


def run_batch(
    plan: BatchPlan,
    *,
    clock: WallClock | None = None,
    max_events: int | None = None,
    persist_checkpoint: bool = True,
) -> list[InternalEnvelope]:
    """Drive the engine to completion over the window, appending to the ledger.

    Seeds (head ``op:"r"`` rows), drains the heap to ``until_us`` (the simulated
    window end) or ``max_events``, appending each pass to the ledger durably
    (INV-GEN-5) under the runtime role so RLS applies. At finalization persists the
    pool snapshots + the checkpoint codec blob (§9.1, the commit-marker rule).
    Returns the produced envelopes for the JSONL writer.
    """
    shard = build_shard(plan, clock)
    sink = LedgerSink(workspace_id=plan.workspace_id)
    produced = shard.run_batch(
        max_events=max_events, until_us=plan.until_us, ledger=sink, pass_size=500
    )
    if persist_checkpoint:
        _finalize(shard, plan)
    return produced


def _finalize(shard: Shard, plan: BatchPlan) -> None:
    """Write pool snapshots first, then the checkpoint row (commit-marker rule)."""
    checkpoint_seq = 1  # first (and only) checkpoint for a batch
    snapshot = SnapshotSink(
        workspace_id=plan.workspace_id, stream_id=plan.stream_id, shard_id=0
    )
    ir = shard.ir
    for entity_type in ir.entity_order:
        pool = shard.pools.pool(entity_type)
        records = (
            pool.records[key].snapshot_json() for key in pool.records
        )
        snapshot.write_pool_image(
            entity_type=entity_type, snapshot_epoch=checkpoint_seq, records=records
        )
    # Serialize the codec blob with the codec's canonical settings, adding a
    # Decimal-aware default: traversal ``memory`` may hold remembered Decimal
    # values (e.g. cart prices) which the codec's plain json.dumps cannot encode.
    # The dict + serialization options are identical to the engine's
    # ``encode_to_json`` (codec §9.1), so the persisted format is unchanged.
    blob = encode_checkpoint(
        shard, checkpoint_seq=checkpoint_seq, config_sha256=plan.config_sha256
    )
    blob_json = json.dumps(blob, separators=(",", ":"), sort_keys=True, default=_json_default)
    write_checkpoint(
        workspace_id=plan.workspace_id,
        stream_id=plan.stream_id,
        shard_id=0,
        checkpoint_seq=checkpoint_seq,
        fencing_token=0,
        blob_json=blob_json,
        last_sequence_no=shard.sequence.last,
        virtual_clock_at=plan.simulated_to,
    )
