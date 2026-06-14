"""Use-case services for the Stream Control context.

Services own the transaction boundary and orchestrate domain models plus infra
adapters (backend-architecture §3.1, application layer). Three responsibilities:

* **create** (T1) — copy the instance pin ``(manifest_version, config_revision →
  merged config)`` and FIX the seed (client-supplied or generated, immutable
  forever, INV-STR-5); seed the single MVP shard row (``shard_id = 0``); audit.
* **start / stop** (T2/T9, idempotent INV-STR-3) — write the *desired* run-state +
  audit; re-issuing the current desired state is a no-op returning current state.
  Control plane writes DESIRED; the runner converges lifecycle.
* **mark_failed** (T4/T11) — the watchdog's terminal transition: no lease within the
  failover window → ``failed`` with ``status_reason = error``/``failover_exhausted``.

Pause/resume FULL semantics are Phase 6 (checkpoint-on-pause, dynamic TPS, WS); the
desired-state write for pause/resume is here as a thin pass-through so the API
surface is complete, but the convergence is stubbed at the runner (Phase 6).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from streams.application import audit, quotas
from streams.domain.models import (
    LC_CREATED,
    LC_FAILED,
    LC_STARTING,
    LC_STOPPED,
    LC_STOPPING,
    MVP_SHARD_COUNT,
    MVP_SHARD_ID,
    REASON_ERROR,
    REASON_FAILOVER_EXHAUSTED,
    REASON_NONE,
    REASON_USER,
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_STOPPED,
    Stream,
    StreamShard,
)

# The R-3 seed domain (api-spec §4.8): [0, 2**63 - 1].
_SEED_MAX = (2**63) - 1

__all__ = [
    "PinDeprecated",
    "StreamCreateInput",
    "StreamNotStartable",
    "StreamQuotaExceeded",
    "create_stream",
    "generate_seed",
    "mark_failed",
    "request_pause",
    "request_resume",
    "request_start",
    "request_stop",
]

# Re-exported for the API layer's except-clauses.
StreamQuotaExceeded = quotas.StreamQuotaExceeded


class PinDeprecated(Exception):
    """The instance's pinned manifest version is deprecated (INV-CAT-5; 409)."""


class StreamNotStartable(Exception):
    """A start was issued from a non-startable lifecycle state (409, T2 guard).

    Start is legal only from ``created``/``stopped``/``failed`` (api-spec §4.8.1);
    e.g. ``start`` while ``pausing`` → 409 invalid-state-transition.
    """


@dataclass(frozen=True)
class StreamCreateInput:
    """Validated create input (the serializer produces this; the service consumes it)."""

    name: str
    scenario_instance_id: UUID
    seed: int | None
    target_tps: int
    chaos_config: dict[str, Any]
    virtual_epoch: Any | None
    speed_multiplier: Any
    clock_mode: str
    backfill_days: int | None


@dataclass(frozen=True)
class _ResolvedPin:
    """The pin copied at create from the scenario instance (T1, INV-CAT-4)."""

    instance_id: UUID
    scenario_slug: str
    manifest_version: str
    scenario_definition_id: UUID
    merged_config: dict[str, Any]
    config_version: int
    pin_sha256: str


def generate_seed() -> int:
    """A server-generated seed in the R-3 domain [0, 2**63 - 1] (api-spec §4.8)."""
    return secrets.randbelow(_SEED_MAX + 1)


def _resolve_instance_pin(instance_id: UUID, *, workspace_id: UUID) -> _ResolvedPin:
    """Resolve + snapshot the pinnable scenario instance into an immutable pin.

    The instance is read under the active workspace context (the scoped manager
    masks foreign instances → not found). The pinned manifest version must be
    published and not deprecated (INV-CAT-5). The merged ``(manifest + overlay)``
    document and its sha256 are computed via the generation engine driver's pure
    helpers (reused — no reimplementation), exactly the determinism unit (PIN-1).
    """
    from catalog.application.services import InstancePinDeprecated, _resolve_pinnable
    from catalog.domain.models import ScenarioInstance
    from config.problems import NotFoundError
    from generation.application.engine_driver import canonical_sha256, merged_document_for

    instance = ScenarioInstance.objects.filter(id=instance_id).first()
    if instance is None:
        raise NotFoundError()  # foreign / unknown instance → 404 (W-3 masking)
    try:
        definition = _resolve_pinnable(
            instance.scenario.slug,
            instance.scenario_definition.version,
            workspace_id=workspace_id,
        )
    except InstancePinDeprecated as exc:
        raise PinDeprecated(str(exc)) from exc
    merged = merged_document_for(definition.manifest, dict(instance.overrides or {}))
    return _ResolvedPin(
        instance_id=instance.id,
        scenario_slug=instance.scenario.slug,
        manifest_version=definition.version,
        scenario_definition_id=definition.id,
        merged_config=merged,
        config_version=instance.config_version,
        pin_sha256=canonical_sha256(merged),
    )


def create_stream(*, workspace: Any, data: StreamCreateInput, actor: Any) -> Stream:
    """T1: create a stream, copying the instance pin and fixing the seed (INV-STR-5).

    Copies ``(manifest_version, config_revision → merged config)`` from the pinned
    scenario instance (INV-CAT-4: streams copy, they never reference) and computes
    the determinism fingerprint ``pin_sha256`` (PIN-1). The seed is fixed now —
    client-supplied or generated — and is immutable for the life of the stream
    (INV-STR-5). The single MVP shard row (``shard_id = 0``) is seeded with
    ``fencing_token = 0``. Created ``status = created``, desired ``stopped`` —
    emission begins at ``start``.
    """
    pin = _resolve_instance_pin(data.scenario_instance_id, workspace_id=workspace.id)
    seed = data.seed if data.seed is not None else generate_seed()
    virtual_epoch = data.virtual_epoch or timezone.now()
    with transaction.atomic():
        stream: Stream = Stream.objects.create(
            workspace=workspace,
            scenario_config_id=pin.instance_id,
            scenario_slug=pin.scenario_slug,
            name=data.name,
            manifest_version=pin.manifest_version,
            scenario_definition_id=pin.scenario_definition_id,
            pinned_config=pin.merged_config,
            pinned_config_version=pin.config_version,
            pin_sha256=pin.pin_sha256,
            seed=seed,
            desired_state=RUN_STOPPED,
            target_tps=data.target_tps,
            chaos_config=data.chaos_config,
            lifecycle_state=LC_CREATED,
            status_reason=REASON_NONE,
            virtual_epoch=virtual_epoch,
            speed_multiplier=data.speed_multiplier,
            clock_mode=data.clock_mode,
            backfill_days=data.backfill_days,
            shard_count=MVP_SHARD_COUNT,
            created_by=getattr(actor, "id", None),
        )
        # Seed the MVP shard registry row (shard_id = 0); fencing_token starts at 0.
        StreamShard.objects.create(
            workspace=workspace,
            stream_id=stream.id,
            shard_id=MVP_SHARD_ID,
            fencing_token=0,
        )
        audit.emit(
            "streams.stream.created",
            actor=actor,
            workspace_id=workspace.id,
            target={"type": "stream", "id": str(stream.id), "label": stream.name},
            metadata={
                "scenario_slug": pin.scenario_slug,
                "manifest_version": pin.manifest_version,
                "config_revision": pin.config_version,
                "pin_sha256": pin.pin_sha256,
            },
        )
    return stream


# --- Lifecycle command handlers (T2/T5/T7/T9; idempotent INV-STR-3) ----------
#
# The control plane writes the DESIRED run-state (and audits); the runner converges
# the lifecycle_state. Each command is idempotent: re-issuing the current desired
# state is a no-op that returns the current state (INV-STR-3, api-spec I-5). The
# lifecycle_state is the runner's to write (with a fencing token), with two control
# -plane exceptions: a fresh start nudges created/stopped/failed → starting so the
# claimable scan and the T4 watchdog have a converging state to observe, and stop
# nudges → stopping so finalize (T10) is reachable. These are advisory nudges the
# runner overwrites under its fencing token within a tick; they are never the
# emission authority (that is the runner, ADR-0006).

# Start is legal only from these lifecycle states (T2/T12/T13; api-spec §4.8.1).
_STARTABLE_FROM = frozenset({LC_CREATED, LC_STOPPED, LC_FAILED})


def _audit_lifecycle(
    action: str, stream: Stream, actor: Any, *, extra: dict[str, Any] | None = None
) -> None:
    audit.emit(
        action,
        actor=actor,
        workspace_id=stream.workspace_id,
        target={"type": "stream", "id": str(stream.id), "label": stream.name},
        metadata={
            "desired_state": stream.desired_state,
            "lifecycle_state": stream.lifecycle_state,
            **(extra or {}),
        },
    )


def request_start(*, stream: Stream, actor: Any) -> Stream:
    """T2/T12/T13: set desired = running (idempotent INV-STR-3).

    Idempotent: ``start`` on an already-``running`` desired stream is a no-op
    returning current state (no audit, no write — re-issuing the current desired
    state is contractually silent). Guarded: start from a non-startable lifecycle
    state (e.g. ``pausing``) → :class:`StreamNotStartable` (409). The TPS + concurrent
    -stream quota caps are checked here (INV-TEN-5). Nudges the lifecycle to
    ``starting`` so the claimable scan picks it up and the T4 watchdog has a clock.
    """
    if stream.desired_state == RUN_RUNNING:
        return stream  # INV-STR-3 no-op: already running-desired
    if stream.lifecycle_state not in _STARTABLE_FROM:
        raise StreamNotStartable(
            f"start is illegal from lifecycle state {stream.lifecycle_state!r} "
            f"(legal only from created/stopped/failed; T2)"
        )
    quotas.check_start_allowed(stream)
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_RUNNING
        stream.lifecycle_state = LC_STARTING
        stream.status_reason = REASON_NONE
        stream.last_transition_at = now
        stream.updated_at = now
        if stream.first_started_at is None:
            stream.first_started_at = now  # pin lock engages (INV-STR-5)
        stream.save(
            update_fields=[
                "desired_state",
                "lifecycle_state",
                "status_reason",
                "last_transition_at",
                "updated_at",
                "first_started_at",
            ]
        )
        _audit_lifecycle("streams.stream.start_requested", stream, actor)
    return stream


def request_stop(*, stream: Stream, actor: Any) -> Stream:
    """T9: set desired = stopped, overriding any in-flight pause/start (idempotent).

    Idempotent: ``stop`` on an already-``stopped`` desired stream is a no-op
    returning current state (INV-STR-3) — this also covers a never-started
    ``created`` stream (its desired is already ``stopped``, so it is not emitting).
    Stop is legal from any non-terminal state and overrides in-flight pause/start
    (T9); it nudges lifecycle → ``stopping`` so the runner reaches finalize (T10).
    """
    if stream.desired_state == RUN_STOPPED:
        return stream  # INV-STR-3 no-op: already stopped-desired (incl. created)
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_STOPPED
        # Nudge to stopping and let the runner finalize (T10). A stream with a
        # non-stopped desired state has been started, so a runner is involved.
        stream.lifecycle_state = LC_STOPPING
        stream.status_reason = REASON_USER
        stream.last_transition_at = now
        stream.updated_at = now
        stream.save(
            update_fields=[
                "desired_state",
                "lifecycle_state",
                "status_reason",
                "last_transition_at",
                "updated_at",
            ]
        )
        _audit_lifecycle("streams.stream.stop_requested", stream, actor)
    return stream


def request_pause(*, stream: Stream, actor: Any) -> Stream:
    """T5: set desired = paused (idempotent INV-STR-3).

    Phase 6 owns the FULL pause semantics (checkpoint-on-pause holding state, T6
    convergence). Phase 5 writes the desired-state + audit so the API surface is
    complete; the runner's pause branch holds the lease and halts (a minimal stub,
    backend-architecture §8.3 "Phase 6"). Idempotent: pause on a paused-desired
    stream is a no-op.
    """
    if stream.desired_state == RUN_PAUSED:
        return stream  # INV-STR-3 no-op
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_PAUSED
        stream.status_reason = REASON_USER
        stream.last_transition_at = now
        stream.updated_at = now
        stream.save(
            update_fields=[
                "desired_state",
                "status_reason",
                "last_transition_at",
                "updated_at",
            ]
        )
        _audit_lifecycle("streams.stream.pause_requested", stream, actor)
    return stream


def request_resume(*, stream: Stream, actor: Any) -> Stream:
    """T7: set desired = running from a paused state (idempotent INV-STR-3).

    Phase 6 owns FULL resume (checkpoint restore, dwell rebase, T8). Phase 5 writes
    the desired-state + audit. Idempotent: resume on a running-desired stream is a
    no-op.
    """
    if stream.desired_state == RUN_RUNNING:
        return stream  # INV-STR-3 no-op
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_RUNNING
        stream.status_reason = REASON_NONE
        stream.last_transition_at = now
        stream.updated_at = now
        stream.save(
            update_fields=[
                "desired_state",
                "status_reason",
                "last_transition_at",
                "updated_at",
            ]
        )
        _audit_lifecycle("streams.stream.resume_requested", stream, actor)
    return stream


def mark_failed(
    *, stream: Stream, reason: str = REASON_ERROR, actor: Any = "system"
) -> Stream:
    """T4/T11: the watchdog's terminal transition to ``failed``.

    Called by the lease-expiry watchdog when no live lease appears within the
    failover window (T4: starting → failed within 60 s, ``status_reason = error``)
    or on failover exhaustion (T11: ``status_reason = failover_exhausted``). The
    desired state is left as the user set it (a failed stream is restartable, T13).
    Idempotent: re-running on an already-``failed`` stream is a no-op.
    """
    if stream.lifecycle_state == LC_FAILED:
        return stream
    now = timezone.now()
    with transaction.atomic():
        stream.lifecycle_state = LC_FAILED
        stream.status_reason = (
            reason if reason in (REASON_ERROR, REASON_FAILOVER_EXHAUSTED) else REASON_ERROR
        )
        stream.last_transition_at = now
        stream.updated_at = now
        stream.save(
            update_fields=[
                "lifecycle_state",
                "status_reason",
                "last_transition_at",
                "updated_at",
            ]
        )
        _audit_lifecycle(
            "streams.stream.failed", stream, actor, extra={"reason": stream.status_reason}
        )
    return stream
