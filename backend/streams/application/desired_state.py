"""The desired-state read service the runner polls (backend-architecture §8.3).

ADR-0006: users change *desired state* on the control-plane API; runners reconcile
toward it. There is no command bus — runners poll Postgres each tick (§7.3). This
module is that read.

The §8.3 reconciliation tick comment is explicit: "poll: one batched Postgres read
per process per tick covers all shards." So the interface is a single batched call
that returns every shard's desired state, not one query per shard:

* :func:`claimable_desired_states` — the **claimable scan** input (§8.2): every
  stream whose desired run-state ∈ {running, paused} or that is in a converging
  lifecycle state, as immutable :class:`DesiredState` value objects. This is the
  set the runner's claimable scan (every 2 s) and per-tick desired poll read from.
* :func:`desired_for` — one stream's desired state (the per-shard ``desired.get``
  lookup in the tick, served from the batch the runner caches per tick).

The returned :class:`DesiredState` carries everything the tick needs to reconcile
without a second read: run-state, target_tps, chaos_config, the pin (seed +
manifest + merged config + sha) and the shard fan-out. It is a frozen dataclass
(no ORM rows escape the application layer — the runner host owns no model imports
beyond this seam). This read is unscoped: the runner is a platform process serving
every workspace's shards, and each returned row carries its ``workspace_id`` so the
runner arms ``workspace_scope(workspace_id)`` per shard (backend-architecture §4.2,
INV-STR-6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from streams.domain.models import (
    LC_PAUSING,
    LC_RESUMING,
    LC_STARTING,
    LC_STOPPING,
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_STOPPED,
    Stream,
)

__all__ = ["DesiredState", "claimable_desired_states", "desired_for"]

# Lifecycle states that are still converging toward a terminal/steady state and
# therefore need a runner reconciling them even if desired = stopped (a stream
# being stopped still needs a worker to run finalize T10). §8.2 claimable scan.
_CONVERGING_LIFECYCLE: frozenset[str] = frozenset(
    {LC_STARTING, LC_PAUSING, LC_RESUMING, LC_STOPPING}
)
# Desired run-states that keep a shard claimable (§8.2): running + paused (a paused
# stream holds its lease per T6, but the desired-state read still surfaces it so a
# crashed paused holder can be re-claimed).
_CLAIMABLE_DESIRED: frozenset[str] = frozenset({RUN_RUNNING, RUN_PAUSED})


@dataclass(frozen=True)
class DesiredState:
    """One stream's desired state + immutable pin — the runner's tick input (§8.3).

    A pure value object: no ORM row escapes the application layer. ``target_tps`` is
    the aggregate desired rate (the runner divides by ``shard_count`` for the
    per-shard bucket rate, §8.3 step 3). The pin block is the determinism unit
    (INV-STR-5): ``seed`` + ``manifest_version`` + ``pinned_config`` + ``pin_sha256``.
    """

    stream_id: UUID
    workspace_id: UUID
    run_state: str
    target_tps: int
    chaos_config: dict[str, Any]
    # Lifecycle (so the runner knows whether it is converging: starting → running).
    lifecycle_state: str
    status_reason: str
    # The immutable pin (determinism unit; INV-STR-5).
    seed: int
    scenario_slug: str
    manifest_version: str
    pinned_config: dict[str, Any]
    pinned_config_version: int
    pin_sha256: str
    # Virtual clock (pinned at start; ADR-0008).
    virtual_epoch: datetime
    speed_multiplier: Decimal
    clock_mode: str
    backfill_days: int | None
    shard_count: int

    @property
    def is_stopped(self) -> bool:
        return self.run_state == RUN_STOPPED

    @property
    def is_paused(self) -> bool:
        return self.run_state == RUN_PAUSED


def _to_desired(stream: Stream) -> DesiredState:
    return DesiredState(
        stream_id=stream.id,
        workspace_id=stream.workspace_id,
        run_state=stream.desired_state,
        target_tps=stream.target_tps,
        chaos_config=dict(stream.chaos_config or {}),
        lifecycle_state=stream.lifecycle_state,
        status_reason=stream.status_reason,
        seed=stream.seed,
        scenario_slug=stream.scenario_slug,
        manifest_version=stream.manifest_version,
        pinned_config=dict(stream.pinned_config or {}),
        pinned_config_version=stream.pinned_config_version,
        pin_sha256=stream.pin_sha256,
        virtual_epoch=stream.virtual_epoch,
        speed_multiplier=stream.speed_multiplier,
        clock_mode=stream.clock_mode,
        backfill_days=stream.backfill_days,
        shard_count=stream.shard_count,
    )


def claimable_desired_states() -> list[DesiredState]:
    """Every stream a runner should be reconciling — ONE batched read (§8.3).

    The single query backing the runner's per-tick desired poll and its 2 s
    claimable scan: streams with desired ∈ {running, paused} **or** still converging
    in lifecycle (so a ``stopping`` stream with desired ``stopped`` is still surfaced
    until its worker runs finalize T10). Returns frozen value objects, not ORM rows.

    Unscoped by design: the runner is a platform process spanning all workspaces;
    each row carries ``workspace_id`` for the runner to arm ``workspace_scope`` per
    shard (INV-STR-6). The ``streams_reconcile_ix`` partial index covers the
    desired ≠ stopped half.
    """
    # tenancy: unscoped — the runner data plane reconciles every workspace's shards;
    # each row carries workspace_id and the runner arms workspace_scope per shard
    # (backend-architecture §4.2 / §8.3; INV-STR-6). The cross-tenant SELECT runs
    # under platform_read_scope so the strict Class T policy admits every workspace's
    # rows to the NOBYPASSRLS runtime role (read-only; WITH CHECK is untouched).
    from django.db.models import Q

    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        rows = Stream.all_objects.filter(
            Q(desired_state__in=_CLAIMABLE_DESIRED)
            | Q(lifecycle_state__in=_CONVERGING_LIFECYCLE)
        )
        return [_to_desired(s) for s in rows]


def desired_for(stream_id: UUID | Any) -> DesiredState | None:
    """One stream's desired state (the per-shard tick lookup; foreign → ``None``).

    Unscoped (runner data-plane read by unique id); the row carries its own
    ``workspace_id``. The runner serves this from the cached batch in steady state;
    the direct read exists for the first tick and tests.
    """
    # tenancy: unscoped — runner data-plane desired-state read by unique stream id,
    # under platform_read_scope so RLS admits any workspace's row (read-only, §8.3).
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        stream = Stream.all_objects.filter(id=stream_id).first()
        return _to_desired(stream) if stream is not None else None
