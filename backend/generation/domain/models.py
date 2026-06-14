"""Domain models for the Generation context (database-schema §5.3-5.5).

Four tables, all tenant-owned (non-null ``workspace_id``, Class-T RLS):

* ``ground_truth_ledger`` (§5.5, ADR-0009) — the append-only canonical event
  store: time-partitioned by ``emitted_at`` (wall clock), daily partitions,
  7-day rolling retention via partition drop. Stores the **internal envelope**
  (all 20 fields + ``_df``, with ``_df.canonical = true``). No FK (C-7): a per-row
  FK at data-plane write rates and FK/partition-drop interactions are
  unacceptable; integrity is app-enforced and ``workspace_id`` is denormalized.
  The PK includes ``emitted_at`` (Postgres requires the partition key inside any
  unique constraint); true global uniqueness of ``(stream_id, shard_id,
  sequence_no)`` is guaranteed by the gapless generator counter.
* ``stream_checkpoints`` (§5.3) — one row per (stream, shard), updated in place:
  the resumable engine checkpoint blob (codec §9.1), zstd-compressed canonical
  JSON. Fenced conditional upsert (lease-driven pause/resume is Phase 5-6; the
  FORMAT + persistence ship now for batch finalization).
* ``entity_pool_snapshots`` (§5.4) — one row per (stream, shard, entity_type):
  the durable Tier-3 pool image, zstd-compressed JSONL (one pooled entity per
  line), stamped with the ``snapshot_epoch`` (= the checkpoint_seq it belongs to,
  the commit-marker rule).
* ``datasets`` (api-spec §4.10) — the workspace-owned backfill batch resource:
  status (queued/generating/ready/failed/expired), the pin echo, the result file
  ref, event/byte counts, and expiry.

Every concrete model sets ``Meta.db_table`` explicitly (rule BE-APP-1, C-2) and
subclasses :class:`~tenancy.domain.scoping.WorkspaceScopedModel` so the
``check_tenancy`` guard's tenant assertions apply (workspace_id field, scoped
manager, RLS migration).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from tenancy.domain.scoping import WorkspaceScopedModel


def _uuid4() -> uuid.UUID:
    return uuid.uuid4()


# --- Dataset status (api-spec §4.10.2 closed enum) --------------------------
DATASET_QUEUED = "queued"
DATASET_GENERATING = "generating"
DATASET_READY = "ready"
DATASET_FAILED = "failed"
DATASET_EXPIRED = "expired"
DATASET_STATUSES: tuple[str, ...] = (
    DATASET_QUEUED,
    DATASET_GENERATING,
    DATASET_READY,
    DATASET_FAILED,
    DATASET_EXPIRED,
)
_DATASET_STATUS_CHOICES: list[tuple[str, str]] = [
    (DATASET_QUEUED, "Queued"),
    (DATASET_GENERATING, "Generating"),
    (DATASET_READY, "Ready"),
    (DATASET_FAILED, "Failed"),
    (DATASET_EXPIRED, "Expired"),
]

# Compression (api-spec §4.10.1).
COMPRESSION_GZIP = "gzip"
COMPRESSION_NONE = "none"
COMPRESSIONS: tuple[str, str] = (COMPRESSION_GZIP, COMPRESSION_NONE)
_COMPRESSION_CHOICES: list[tuple[str, str]] = [
    (COMPRESSION_GZIP, "gzip"),
    (COMPRESSION_NONE, "none"),
]


class GroundTruthLedger(WorkspaceScopedModel):
    """The canonical event store (database-schema §5.5; ADR-0009; INV-GEN-5).

    Append-only, time-partitioned by ``emitted_at`` (RANGE, daily). The Django
    model declares the table shape; the partition parent + per-partition index
    template are applied by the migration (``PARTITION BY RANGE (emitted_at)``)
    and individual partitions are owned by the partition manager (M-5,
    ``generation.infra.partitions``). No FK (C-7).

    ``envelope`` is the full internal shape (20 fields + ``_df``) as JSONB; the
    extracted scalar columns (``event_type``/``occurred_at``/…) mirror the
    envelope for indexed query (event-model §8 / M-8 additive evolution only).
    """

    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,  # no real FK at the DB (C-7); see Meta.managed note
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    stream_id = models.UUIDField()
    shard_id = models.IntegerField()
    sequence_no = models.BigIntegerField()  # gapless per (stream, shard) (INV-GEN-7)
    event_id = models.UUIDField()  # deterministic UUIDv7 (event-model §2.2.1)
    event_type = models.TextField()
    occurred_at = models.DateTimeField()  # simulated
    emitted_at = models.DateTimeField()  # wall; partition key
    envelope = models.JSONField()  # internal shape, all 20 fields + _df

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "ground_truth_ledger"  # C-2: database-schema §5.5
        # The PK includes emitted_at (the partition key must be inside any unique
        # constraint on a partitioned table). Django needs a single-column PK for
        # the ORM; the real composite PK is created by the partition-parent DDL in
        # the migration. We mark the model unmanaged for table creation (the raw
        # CREATE TABLE … PARTITION BY does it) but keep it managed for the ORM.
        constraints: ClassVar[list[models.BaseConstraint]] = []
        indexes: ClassVar[list[models.Index]] = []

    def __str__(self) -> str:
        return f"{self.stream_id}:{self.shard_id}:{self.sequence_no}"


class StreamCheckpoint(WorkspaceScopedModel):
    """A resumable engine checkpoint (database-schema §5.3; behavior-engine §9.1).

    One row per (stream, shard), updated in place (single-row upsert): a restore
    point, not a history (history re-derives identically from the seed, INV-GEN-3).
    ``state`` is the zstd-compressed canonical-JSON checkpoint blob (≤ 32 MiB
    compressed). Written at batch finalization this phase; the fenced conditional
    upsert (``fencing_token``/``checkpoint_seq``) supports lease-driven
    pause/resume in Phase 5-6.
    """

    stream_id = models.UUIDField()
    shard_id = models.IntegerField()
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    checkpoint_seq = models.BigIntegerField()  # monotonic write counter
    fencing_token = models.BigIntegerField(default=0)  # writer's token (§5.3)
    state = models.BinaryField()  # zstd-compressed canonical JSON
    state_format = models.IntegerField(default=1)  # payload layout version
    last_sequence_no = models.BigIntegerField()  # envelope sequence_no high-water
    virtual_clock_at = models.DateTimeField()  # simulated position at checkpoint
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "stream_checkpoints"  # C-2: database-schema §5.3
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # PRIMARY KEY (stream_id, shard_id) — modeled as a unique constraint so
            # the ORM keeps its own surrogate id PK; the conditional fenced upsert
            # targets this constraint (generation.infra.checkpoint_store).
            models.UniqueConstraint(
                fields=["stream_id", "shard_id"], name="stream_checkpoints_pk"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace"], name="stream_checkpoints_ws_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.stream_id}:{self.shard_id}@{self.checkpoint_seq}"


class EntityPoolSnapshot(WorkspaceScopedModel):
    """A durable Tier-3 pool image (database-schema §5.4; behavior-engine §4.1/§9.1).

    One row per (stream, shard, entity_type), upserted with the same fencing
    condition as checkpoints. ``payload`` is zstd-compressed JSONL — one pooled
    entity per line (the ``snapshot_json`` shape). ``snapshot_epoch`` = the
    ``checkpoint_seq`` this snapshot belongs to (the commit-marker rule): a
    snapshot with ``snapshot_epoch > checkpoint_seq`` (crash mid-cycle) is ignored
    on restore and overwritten next cycle.
    """

    stream_id = models.UUIDField()
    shard_id = models.IntegerField()
    entity_type = models.TextField()  # manifest entity name
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    snapshot_epoch = models.BigIntegerField()  # = the checkpoint_seq it belongs to
    fencing_token = models.BigIntegerField(default=0)
    payload = models.BinaryField()  # zstd-compressed JSONL, one entity per line
    entity_count = models.IntegerField()
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "entity_pool_snapshots"  # C-2: database-schema §5.4
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["stream_id", "shard_id", "entity_type"],
                name="entity_pool_snapshots_pk",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace"], name="entity_pool_snapshots_ws_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.stream_id}:{self.shard_id}:{self.entity_type}@{self.snapshot_epoch}"


class Dataset(WorkspaceScopedModel):
    """A backfill batch dataset (api-spec §4.10; ADR-0008; Phase 4).

    N simulated days of history materialized as a downloadable JSONL file. A
    standard Class-T tenant model. The pin echo (``pin_sha256``, ``seed``,
    ``simulated_window``) makes a dataset reproducible: regenerating with the same
    seed + pin yields a byte-identical dataset (INV-G-4). ``download_path`` /
    ``file_path`` are the artifact refs; the file is deleted at ``expires_at``
    (status → ``expired``) and the file moves to object storage in Phase 11.
    """

    id = models.UUIDField(primary_key=True, default=_uuid4, editable=False)
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.RESTRICT,
        db_column="workspace_id",
        related_name="datasets",
    )
    # The pinned instance (cross-app reference; no ORM relation per app layering).
    scenario_instance_id = models.UUIDField()
    name = models.TextField()
    status = models.TextField(choices=_DATASET_STATUS_CHOICES, default=DATASET_QUEUED)
    progress = models.FloatField(default=0.0)
    seed = models.BigIntegerField()
    # The synthetic stream id the batch generation writes its ledger rows under.
    stream_id = models.UUIDField(default=_uuid4, editable=False)
    pin_sha256 = models.TextField(default="")  # echo of (manifest_version, config_revision)
    simulated_from = models.DateTimeField()
    simulated_to = models.DateTimeField()
    estimated_events = models.BigIntegerField(default=0)
    event_count = models.BigIntegerField(null=True, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    compression = models.TextField(choices=_COMPRESSION_CHOICES, default=COMPRESSION_GZIP)
    file_path = models.TextField(default="")  # local artifact path (Phase 11: object storage)
    failure_reason = models.TextField(default="")
    created_by = models.UUIDField(null=True, blank=True)  # FK → users.id (cross-app)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    ready_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "datasets"  # C-2
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(status__in=DATASET_STATUSES),
                name="datasets_status_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(seed__gte=0),
                name="datasets_seed_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "status"], name="datasets_ws_status_ix"),
            models.Index(fields=["workspace", "-created_at"], name="datasets_ws_created_ix"),
        ]

    def __str__(self) -> str:
        return self.name
