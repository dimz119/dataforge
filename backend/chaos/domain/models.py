"""Domain models for the Chaos context (database-schema §6.2-6.3).

Two tenant-owned tables, both denormalizing ``workspace_id`` (Class-T RLS, C-8)
and carrying no FK (C-7 — data-plane write rates):

* ``chaos_injections`` (§6.2, ADR-0017) — the append-only answer-key store: one
  row per injection (the :class:`~dataforge_engine.chaos.InjectionRecord`
  aggregate), written BEFORE the affected instance is published/suppressed
  (INV-CHA-4). Time-partitioned by ``recorded_at`` in production; the Django model
  declares the table shape and a single-column surrogate PK for the ORM (the real
  composite PK ``(injection_id, recorded_at)`` is created by the partitioned-parent
  DDL — out of scope here, modelled flat on the unit lane).
* ``late_arrival_buffer`` (§6.3, INV-CHA-5) — the DURABLE schedule of pending
  ``late_arriving`` re-emissions: pending entries survive pause and runner failover
  because they live here, not in process memory. Non-partitioned; each row holds a
  full copy of the internal envelope (JSONB) so re-emission never reads the ledger.

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

__all__ = ["ChaosInjection", "LateArrivalBufferEntry"]

# The seven ChaosMode identifiers (domain-model §2.7; DDL check constraint §6.2).
_CHAOS_MODES: tuple[str, ...] = (
    "duplicates",
    "late_arriving",
    "missing",
    "out_of_order",
    "corrupted_values",
    "nulls",
    "schema_drift",
)

# Late-buffer states (frozen DDL check constraint, database-schema §6.3).
BUFFER_PENDING = "pending"
BUFFER_EMITTED = "emitted"
BUFFER_DISCARDED = "discarded"
_BUFFER_STATES: tuple[str, ...] = (BUFFER_PENDING, BUFFER_EMITTED, BUFFER_DISCARDED)


def _uuid7_placeholder() -> uuid.UUID:
    """Surrogate id default. Real entries supply a deterministic UUIDv7 id."""
    return uuid.uuid4()


class ChaosInjection(WorkspaceScopedModel):
    """One ``chaos_injections`` answer-key row (database-schema §6.2; §7.1).

    ``details`` mirrors the ``_df.chaos`` shapes per mode (§5.1-5.7); for
    ``late_arriving`` it carries ``delay_simulated_ms``, ``due_at_wall``,
    ``outcome`` (``pending`` → ``emitted``/``flushed``/``discarded``),
    ``realized_wall_delay_ms`` (set at finalization), and ``duplicate_index``.
    """

    injection_id = models.UUIDField(primary_key=True, default=_uuid7_placeholder, editable=False)
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    stream_id = models.UUIDField()
    shard_id = models.IntegerField()
    mode = models.TextField()
    event_id = models.UUIDField()
    sequence_no = models.BigIntegerField()
    occurred_at = models.DateTimeField()  # simulated; copied from canonical event
    canonical_emitted_at = models.DateTimeField()  # wall; canonical instance emitted_at
    details = models.JSONField(default=dict)
    recorded_at = models.DateTimeField(default=timezone.now)  # wall; partition key

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "chaos_injections"  # C-2: database-schema §6.2
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(mode__in=_CHAOS_MODES),
                name="chaos_injections_mode_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["stream_id", "mode", "recorded_at"],
                name="chaos_inj_stream_mode_ix",
            ),
            models.Index(fields=["stream_id", "event_id"], name="chaos_injections_event_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.mode}:{self.event_id}"


class LateArrivalBufferEntry(WorkspaceScopedModel):
    """One ``late_arrival_buffer`` pending re-emission (database-schema §6.3).

    Durable Postgres state (INV-CHA-5): ``pending`` rows survive pause and failover.
    ``envelope`` is the full INTERNAL envelope (incl. ``_df.chaos.late_arriving``)
    so re-emission is self-contained. ``state`` is publish-then-flip: the scheduler
    publishes with ``emitted_at := now()`` and flips to ``emitted`` (§6.2).
    """

    id = models.UUIDField(primary_key=True, default=_uuid7_placeholder, editable=False)
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    stream_id = models.UUIDField()
    shard_id = models.IntegerField()
    injection_id = models.UUIDField()  # logical ref to chaos_injections (no FK, C-7)
    event_id = models.UUIDField()
    envelope = models.JSONField()  # internal shape incl. _df late-arrival labels
    due_at = models.DateTimeField()  # wall (event-model §3.4)
    state = models.TextField(default=BUFFER_PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)  # when state left pending

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "late_arrival_buffer"  # C-2: database-schema §6.3
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(state__in=_BUFFER_STATES),
                name="late_buffer_state_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["due_at"],
                name="late_buffer_due_ix",
                condition=models.Q(state="pending"),
            ),
            models.Index(fields=["stream_id", "state"], name="late_buffer_stream_ix"),
            models.Index(fields=["workspace"], name="late_buffer_ws_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.stream_id}:{self.event_id}@{self.due_at:%Y%m%d%H%M%S}({self.state})"
