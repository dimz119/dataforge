"""Domain models for the Delivery context (database-schema §6.1).

``event_buffer`` — the REST delivery buffer (ADR-0013): post-chaos events in the
**delivered shape** (``_df`` stripped at sink ingestion, event-model §5.2), written
by the buffer-writer Kafka consumer (delivery-channels §4). Time-partitioned by
``partition_ts`` (wall clock, write-assigned) on UTC-hour boundaries; physical
retention 48 h for all plans via partition drop, the Free plan's 24 h window
enforced logically at read time. No FK (C-7) — per-row FK at data-plane write rates
and FK/partition-drop interactions are unacceptable; integrity is app-enforced and
``workspace_id`` is denormalized onto every row (INV-TEN-1, C-8).

The Django model declares the table shape; the partition parent + per-partition
index/RLS template are applied by the migration (``PARTITION BY RANGE
(partition_ts)``) and individual partitions are owned by the partition manager
(``delivery.infra.partitions``). The PK is ``(stream_id, partition_ts,
buffer_seq)`` — Postgres requires the partition key inside any unique constraint;
``buffer_seq`` is the per-stream monotonic append counter the single writer assigns
(BW-6/BW-7), so ``(partition_ts, buffer_seq)`` order is identical to ``buffer_seq``
order and every page read prunes partitions (INV-DEL-3 replay stability).

Every concrete model sets ``Meta.db_table`` explicitly (rule BE-APP-1, C-2) and
subclasses :class:`~tenancy.domain.scoping.WorkspaceScopedModel` so the
``check_tenancy`` guard's tenant assertions apply (workspace_id field, scoped
manager, RLS migration).
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models

from tenancy.domain.scoping import WorkspaceScopedModel

__all__ = ["EventBuffer"]


class EventBuffer(WorkspaceScopedModel):
    """The REST delivery buffer (database-schema §6.1; delivery-channels §4; ADR-0013).

    Append-only, immutable (no UPDATE surface, BW-6), time-partitioned by
    ``partition_ts`` (RANGE, hourly). ``envelope`` is the **delivered shape**
    exactly — the 20 contract fields post-strip (SB-2/BW-5) — and the extracted
    scalar columns mirror it for indexed query + the cursor page query (§6.1).
    No FK (C-7).

    The writer never deduplicates on ``event_id`` (BW-4): chaos duplicates are
    distinct delivered instances and are all stored (SINK-4); a crash-window
    redelivery (BW-3) appends duplicate rows under fresh ``buffer_seq`` values —
    the rare server-side duplicate the channel's at-least-once contract licenses.
    """

    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,  # no real FK at the DB (C-7); see model docstring
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    stream_id = models.UUIDField()
    partition_ts = models.DateTimeField()  # wall; partition key; assigned at write
    buffer_seq = models.BigIntegerField()  # per-stream monotonic append counter
    event_id = models.UUIDField()  # delivered idempotency key; NOT deduplicated (BW-4)
    event_type = models.TextField()
    occurred_at = models.DateTimeField()  # simulated
    emitted_at = models.DateTimeField()  # wall (post-chaos; lateness already applied)
    # The delivered shape (exactly the 20 contract fields) stored as canonical-JSON
    # TEXT — not jsonb — so the bytes survive verbatim for S-3 cross-channel identity
    # (jsonb would reorder keys / normalize numbers and break byte-equality, BW-5).
    envelope = models.TextField()

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "event_buffer"  # C-2: database-schema §6.1
        # The real composite PK (stream_id, partition_ts, buffer_seq) is created by
        # the partitioned-parent DDL in the migration (the partition key must sit
        # inside any unique constraint). The ORM keeps its own surrogate id PK; the
        # cursor page query targets the composite via raw row-comparison (§6.1).
        constraints: ClassVar[list[models.BaseConstraint]] = []
        indexes: ClassVar[list[models.Index]] = []

    def __str__(self) -> str:
        return f"{self.stream_id}@{self.partition_ts:%Y%m%d%H}:{self.buffer_seq}"
