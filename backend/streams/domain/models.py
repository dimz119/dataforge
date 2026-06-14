"""Domain models for the Stream Control context (database-schema §5.1-5.2).

Two tenant-owned tables (non-null ``workspace_id``, Class-T RLS):

* ``streams`` (§5.1, domain-model §2.5) — the Stream aggregate root. Persists the
  user-settable **desired state** (``desired_state`` ∈ running|paused|stopped,
  ``target_tps``, ``chaos_config``) that runners reconcile toward (ADR-0006), the
  runner-converged ``lifecycle_state`` + ``status_reason``, the immutable
  determinism pin ``(manifest_version, scenario_definition_id, pinned_config,
  pinned_config_version, seed)`` copied at create (T1, INV-STR-5), and the
  virtual-clock configuration (pinned, ADR-0008). The control plane writes
  desired state + lifecycle commands; the runner writes ``lifecycle_state`` under
  a fencing token (§8.2). MVP: ``shard_count = 1``.
* ``stream_shards`` (§5.2) — the durable shard registry + the **fencing-token
  authority**. Leases live in Redis (TTL'd heartbeats); Postgres holds the
  monotonic ``fencing_token`` counter (incremented transactionally on every
  acquisition so a flushed Redis can never let an old runner win a token race)
  plus an advisory audit copy of the last lease transition (never authoritative).
  MVP: one row per stream at ``shard_id = 0``.

Every concrete model sets ``Meta.db_table`` explicitly (rule BE-APP-1, C-2) and
subclasses :class:`~tenancy.domain.scoping.WorkspaceScopedModel` so the
``check_tenancy`` guard's tenant assertions apply (workspace_id field, scoped
manager, RLS migration). No cross-app ORM relations beyond the workspace FK
(``scenario_config_id`` / ``scenario_definition_id`` are denormalized UUIDs, the
import-linter cross-app rule).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from tenancy.domain.scoping import WorkspaceScopedModel


def _uuid4() -> uuid.UUID:
    return uuid.uuid4()


# --- Desired run-state (database-schema §5.1; domain-model §2.5 closed enum) ---
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_STOPPED = "stopped"
RUN_STATES: tuple[str, ...] = (RUN_RUNNING, RUN_PAUSED, RUN_STOPPED)
_RUN_STATE_CHOICES: list[tuple[str, str]] = [
    (RUN_RUNNING, "Running"),
    (RUN_PAUSED, "Paused"),
    (RUN_STOPPED, "Stopped"),
]

# --- Lifecycle state (database-schema §5.1; domain-model §4.2-4.3 closed enum) -
LC_CREATED = "created"
LC_STARTING = "starting"
LC_RUNNING = "running"
LC_PAUSING = "pausing"
LC_PAUSED = "paused"
LC_RESUMING = "resuming"
LC_STOPPING = "stopping"
LC_STOPPED = "stopped"
LC_FAILED = "failed"
LIFECYCLE_STATES: tuple[str, ...] = (
    LC_CREATED,
    LC_STARTING,
    LC_RUNNING,
    LC_PAUSING,
    LC_PAUSED,
    LC_RESUMING,
    LC_STOPPING,
    LC_STOPPED,
    LC_FAILED,
)
_LIFECYCLE_CHOICES: list[tuple[str, str]] = [(s, s.title()) for s in LIFECYCLE_STATES]

# The three states a stream may be deleted from (T14).
DELETABLE_STATES: frozenset[str] = frozenset({LC_CREATED, LC_STOPPED, LC_FAILED})

# --- Status reason (database-schema §5.1; domain-model §2.5 closed enum) -------
REASON_NONE = "none"
REASON_USER = "user"
REASON_QUOTA = "quota"
REASON_IDLE = "idle"
REASON_ERROR = "error"
REASON_FAILOVER_EXHAUSTED = "failover_exhausted"
STATUS_REASONS: tuple[str, ...] = (
    REASON_NONE,
    REASON_USER,
    REASON_QUOTA,
    REASON_IDLE,
    REASON_ERROR,
    REASON_FAILOVER_EXHAUSTED,
)
_REASON_CHOICES: list[tuple[str, str]] = [(r, r.replace("_", " ").title()) for r in STATUS_REASONS]

# --- Virtual-clock mode (database-schema §5.1; ADR-0008 closed enum) ----------
CLOCK_LIVE = "live"
CLOCK_BACKFILL = "backfill"
CLOCK_MODES: tuple[str, ...] = (CLOCK_LIVE, CLOCK_BACKFILL)
_CLOCK_MODE_CHOICES: list[tuple[str, str]] = [(CLOCK_LIVE, "Live"), (CLOCK_BACKFILL, "Backfill")]

# MVP shard fan-out (database-schema §5.1; domain-model §2.5 "MVP: 1 shard").
MVP_SHARD_COUNT = 1
MVP_SHARD_ID = 0


class Stream(WorkspaceScopedModel):
    """The Stream aggregate root (database-schema §5.1; domain-model §2.5).

    Desired state (``desired_state``/``target_tps``/``chaos_config``) is
    user-settable on the control-plane API; runners reconcile toward it (ADR-0006).
    ``lifecycle_state`` + ``status_reason`` are runner-converged (the runner writes
    them under a fencing token, §8.2; the control plane writes ``failed`` via the
    watchdog, T4/T11). The pin block is immutable once ``first_started_at`` is set
    (INV-STR-5) — enforced at the service/serializer layer, the API has no mutation
    path (domain-model §4.4). ``scenario_config_id``/``scenario_definition_id`` are
    cross-app references stored as bare UUIDs (no ORM relation).
    """

    id = models.UUIDField(primary_key=True, default=_uuid4, editable=False)
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.RESTRICT,
        db_column="workspace_id",
        related_name="streams",
    )
    # Cross-app references (no ORM relation; import-linter cross-app rule, C-8).
    scenario_config_id = models.UUIDField()  # → workspace_scenario_configs.id
    scenario_slug = models.TextField()  # denormalized for envelope stamping (§5.1)
    name = models.TextField()

    # --- determinism pin (immutable once first started; INV-STR-5) ---
    manifest_version = models.TextField()  # semver of the pinned scenario_definition
    scenario_definition_id = models.UUIDField()  # → scenario_definitions.id
    pinned_config = models.JSONField(default=dict)  # merged manifest+overlay snapshot
    pinned_config_version = models.IntegerField(default=1)  # config_revision copied
    pin_sha256 = models.TextField(default="")  # determinism fingerprint (PIN-1)
    seed = models.BigIntegerField()  # fixed at create; never re-rolled (INV-STR-5)

    # --- desired state (user-settable; runners reconcile — ADR-0006) ---
    desired_state = models.TextField(choices=_RUN_STATE_CHOICES, default=RUN_STOPPED)
    target_tps = models.IntegerField(default=10)
    chaos_config = models.JSONField(default=dict)  # live ChaosPolicy; chaos-engine owns shape
    schema_version_pins = models.JSONField(default=dict)  # {subject: version}; empty = latest
    schema_upgrade_schedule = models.JSONField(null=True, blank=True)  # Phase 10 surface

    # --- lifecycle (runner-converged; domain-model §4.2-4.3) ---
    lifecycle_state = models.TextField(choices=_LIFECYCLE_CHOICES, default=LC_CREATED)
    status_reason = models.TextField(choices=_REASON_CHOICES, default=REASON_NONE)

    # --- virtual clock (pinned at start; ADR-0008) ---
    virtual_epoch = models.DateTimeField()  # simulated epoch
    speed_multiplier = models.DecimalField(max_digits=8, decimal_places=2, default=1)
    clock_mode = models.TextField(choices=_CLOCK_MODE_CHOICES, default=CLOCK_LIVE)
    backfill_days = models.IntegerField(null=True, blank=True)

    shard_count = models.IntegerField(default=MVP_SHARD_COUNT)
    created_by = models.UUIDField()  # → users.id (cross-app)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)
    first_started_at = models.DateTimeField(null=True, blank=True)
    last_transition_at = models.DateTimeField(null=True, blank=True)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "streams"  # C-2: database-schema §5.1
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(desired_state__in=RUN_STATES), name="streams_desired_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(target_tps__gte=1) & models.Q(target_tps__lte=100000),
                name="streams_tps_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(lifecycle_state__in=LIFECYCLE_STATES),
                name="streams_lifecycle_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(status_reason__in=STATUS_REASONS), name="streams_reason_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(clock_mode__in=CLOCK_MODES), name="streams_clock_mode_ck"
            ),
            models.CheckConstraint(condition=models.Q(seed__gte=0), name="streams_seed_ck"),
            models.CheckConstraint(
                condition=models.Q(shard_count__gte=1) & models.Q(shard_count__lte=64),
                name="streams_shard_count_ck",
            ),
            models.CheckConstraint(
                # backfill_days is set iff clock_mode = backfill (§5.1).
                condition=(
                    (models.Q(clock_mode=CLOCK_BACKFILL) & models.Q(backfill_days__isnull=False))
                    | (models.Q(clock_mode=CLOCK_LIVE) & models.Q(backfill_days__isnull=True))
                ),
                name="streams_backfill_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "lifecycle_state"], name="streams_ws_ix"),
            # The control-plane convergence scan: streams whose desired ≠ stopped.
            models.Index(
                fields=["desired_state", "lifecycle_state"],
                name="streams_reconcile_ix",
                condition=~models.Q(desired_state=RUN_STOPPED),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.lifecycle_state})"

    @property
    def status(self) -> str:
        """The surfaced status string (domain-model §4.3, "surfaced status string").

        The lifecycle state, suffixed with the reason when it is informative
        (``paused_quota``, ``paused_idle``) — the PRD §7 contract.
        """
        if self.lifecycle_state == LC_PAUSED and self.status_reason in (REASON_QUOTA, REASON_IDLE):
            return f"{LC_PAUSED}_{self.status_reason}"
        return self.lifecycle_state

    @property
    def is_deletable(self) -> bool:
        """True iff the stream is in a deletable lifecycle state (T14)."""
        return self.lifecycle_state in DELETABLE_STATES

    @property
    def is_pin_locked(self) -> bool:
        """True once the stream has been started — the pin is then immutable (INV-STR-5)."""
        return self.first_started_at is not None


class StreamShard(WorkspaceScopedModel):
    """The durable shard registry + fencing-token authority (database-schema §5.2).

    Leases are Redis-only (TTL'd heartbeats); this table is **never** the lease
    authority. It holds the monotonic ``fencing_token`` (incremented transactionally
    on every acquisition — Postgres is the token authority so a flushed Redis can
    never let an old runner win a token race, INV-STR-2) plus an advisory audit copy
    of the last lease transition (observability; ``last_runner_id`` /
    ``last_acquired_at`` / ``last_released_at``). MVP: one row per stream at
    ``shard_id = 0``.

    The real PK is the composite ``(stream_id, shard_id)``; the ORM keeps its own
    surrogate ``id`` and the composite is a unique constraint (mirrors
    ``stream_checkpoints``). The checkpoint FK references this ``(stream_id, shard_id)``
    pair (database-schema §5.3) — created by the generation migration, not here.
    """

    stream_id = models.UUIDField()  # → streams.id (same app; bare UUID, no relation)
    shard_id = models.IntegerField()
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.DO_NOTHING,
        db_column="workspace_id",
        db_constraint=False,
        related_name="+",
    )
    fencing_token = models.BigIntegerField(default=0)  # durable monotonic counter (INV-STR-2)
    # Advisory audit copy of the Redis lease (observability; never authoritative).
    last_runner_id = models.TextField(null=True, blank=True)
    last_acquired_at = models.DateTimeField(null=True, blank=True)
    last_released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "stream_shards"  # C-2: database-schema §5.2
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["stream_id", "shard_id"], name="stream_shards_pk"
            ),
            models.CheckConstraint(
                condition=models.Q(shard_id__gte=0), name="stream_shards_shard_ck"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace"], name="stream_shards_ws_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.stream_id}:{self.shard_id}@token={self.fencing_token}"
