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

* **pause / resume** (T5/T7, idempotent INV-STR-3) — write the *desired* run-state
  ``paused``/``running`` + ``status_reason`` {user|quota|idle|error} + audit. The
  runner converges (T6 checkpoint-on-pause holding warm state; T8 restore + dwell
  rebase). System pauses (quota/idle TRIGGERS) land Phase 11; the ``status_reason``
  plumbing that renders ``paused_quota``/``paused_idle`` exists now (T5 reason arg).
* **set_target_tps** (PATCH, PIN-3 live mutation) — write the *desired* ``target_tps``
  (1-1000, quota-capped at command time INV-TEN-5) + audit; the runner picks it up on
  the next desired-state poll → effective ≤ 2 s (the Phase 6 exit criterion).
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
    LC_PAUSED,
    LC_PAUSING,
    LC_RESUMING,
    LC_RUNNING,
    LC_STARTING,
    LC_STOPPED,
    LC_STOPPING,
    MVP_SHARD_COUNT,
    MVP_SHARD_ID,
    REASON_ERROR,
    REASON_FAILOVER_EXHAUSTED,
    REASON_IDLE,
    REASON_NONE,
    REASON_QUOTA,
    REASON_USER,
    RUN_PAUSED,
    RUN_RUNNING,
    RUN_STOPPED,
    Stream,
    StreamShard,
)

# The status_reason values a pause may carry (domain-model §4.3 T5; rendered as
# paused_quota/paused_idle by Stream.status). user = explicit user pause; quota/idle
# = system TRIGGERS (Phase 11) — the reason plumbing exists now.
PAUSE_REASONS: frozenset[str] = frozenset({REASON_USER, REASON_QUOTA, REASON_IDLE, REASON_ERROR})

# The R-3 seed domain (api-spec §4.8): [0, 2**63 - 1].
_SEED_MAX = (2**63) - 1

__all__ = [
    "PinDeprecated",
    "StreamCreateInput",
    "StreamNotPausable",
    "StreamNotResumable",
    "StreamNotStartable",
    "StreamQuotaExceeded",
    "create_stream",
    "generate_seed",
    "mark_failed",
    "request_pause",
    "request_rename",
    "request_resume",
    "request_set_target_tps",
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


class StreamNotPausable(Exception):
    """A pause was issued from a non-pausable state (409, T5 guard, api-spec §4.8.1).

    Pause is legal only from ``running`` (or already ``pausing``/``paused`` →
    idempotent no-op). Pausing a ``created``/``stopped``/``failed`` stream is illegal.
    """


class StreamNotResumable(Exception):
    """A resume was issued from a non-resumable state (409, T7 guard, api-spec §4.8.1).

    Resume is legal only from ``paused``/``pausing`` (or already ``running``/``resuming``
    → idempotent no-op). Resuming a stream that is not paused is illegal.
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


# Pause is legal from these lifecycle states (T5; api-spec §4.8.1 "from running").
# A stream already pausing/paused short-circuits to the idempotent no-op below.
_PAUSABLE_FROM = frozenset({LC_RUNNING, LC_STARTING, LC_RESUMING})
# Resume is legal from these lifecycle states (T7; api-spec §4.8.1 "from paused*").
_RESUMABLE_FROM = frozenset({LC_PAUSED, LC_PAUSING})


def request_pause(
    *, stream: Stream, actor: Any, reason: str = REASON_USER
) -> Stream:
    """T5: set desired = paused; runner converges (T6) — idempotent (INV-STR-3).

    The control plane writes desired ``paused`` + ``status_reason`` and audits; the
    runner halts emission within one tick, persists a checkpoint synchronously, holds
    the lease, and reports ``paused`` (T6). Idempotent: pause on a paused-desired
    stream is a silent no-op returning current state. Guarded: pause is legal only
    from a live lifecycle (``running``/``starting``/``resuming``) — pausing a
    ``created``/``stopped``/``failed`` stream → :class:`StreamNotPausable` (409).

    ``reason`` is the §4.3 T5 status_reason: ``user`` (explicit user pause) renders
    plain ``paused``; ``quota``/``idle`` (system TRIGGERS, Phase 11) render
    ``paused_quota``/``paused_idle`` via :pyattr:`Stream.status`. System pauses audit
    (the call carries ``actor="system"``).
    """
    if reason not in PAUSE_REASONS:
        reason = REASON_USER
    if stream.desired_state == RUN_PAUSED:
        return stream  # INV-STR-3 no-op: already paused-desired
    if stream.lifecycle_state not in _PAUSABLE_FROM:
        raise StreamNotPausable(
            f"pause is illegal from lifecycle state {stream.lifecycle_state!r} "
            f"(legal only from running/starting/resuming; T5)"
        )
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_PAUSED
        stream.lifecycle_state = LC_PAUSING  # nudge → pausing; runner converges to paused (T6)
        stream.status_reason = reason
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
        _audit_lifecycle("streams.stream.pause_requested", stream, actor, extra={"reason": reason})
    return stream


def request_resume(*, stream: Stream, actor: Any) -> Stream:
    """T7: set desired = running from a paused state; runner converges (T8) — idempotent.

    The control plane writes desired ``running`` (clearing the pause reason) + audits;
    the runner restores actor/session machines from the checkpoint, rebases dwell
    timers to the resumed virtual clock, and continues in-flight funnels with zero
    ``sequence_no`` gaps (T8). Idempotent: resume on a running-desired stream is a
    silent no-op. Guarded: resume is legal only from ``paused``/``pausing`` →
    :class:`StreamNotResumable` (409) otherwise. If the pause reason was ``quota``,
    the quota-headroom guard (T7) applies — checked here at command time (INV-TEN-5).
    """
    if stream.desired_state == RUN_RUNNING:
        return stream  # INV-STR-3 no-op: already running-desired
    if stream.lifecycle_state not in _RESUMABLE_FROM:
        raise StreamNotResumable(
            f"resume is illegal from lifecycle state {stream.lifecycle_state!r} "
            f"(legal only from paused/pausing; T7)"
        )
    if stream.status_reason == REASON_QUOTA:
        # T7 quota guard: resuming a quota-paused stream requires restored headroom.
        quotas.check_start_allowed(stream)
    now = timezone.now()
    with transaction.atomic():
        stream.desired_state = RUN_RUNNING
        stream.lifecycle_state = LC_RESUMING  # nudge → resuming; runner converges to running (T8)
        stream.status_reason = REASON_NONE
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
        _audit_lifecycle("streams.stream.resume_requested", stream, actor)
    return stream


def request_set_target_tps(*, stream: Stream, target_tps: int, actor: Any) -> Stream:
    """PATCH live mutation: set desired ``target_tps`` (PIN-3, quota-capped INV-TEN-5).

    Writes the desired ``target_tps`` (the live-mutable §4.4 slot) + audits; the
    runner's token bucket adopts the new wall-rate and the engine re-integrates the
    next arrival at the new density on the next desired-state poll → effective ≤ 2 s
    (the Phase 6 exit criterion). The recorded value is the determinism input
    (behavior-engine §3.6 BE-P4): replays of the same stream read the same schedule
    and stay byte-identical.

    The serializer bounds the value 1..1,000 (out of range → 400 upstream). The
    *plan* per-stream TPS cap is checked here at command time (INV-TEN-5) → a value
    above the cap raises :class:`StreamQuotaExceeded` (403). Idempotent: setting the
    current ``target_tps`` is a silent no-op returning current state (INV-STR-3 in
    spirit — re-issuing the current desired value).
    """
    cap = quotas.per_stream_tps_cap(stream.workspace_id)
    if target_tps > cap:
        raise quotas.StreamQuotaExceeded(
            quota="per_stream_tps", limit=cap, requested=target_tps
        )
    if stream.target_tps == target_tps:
        return stream  # no-op: re-issuing the current desired target_tps
    now = timezone.now()
    with transaction.atomic():
        previous = stream.target_tps
        stream.target_tps = target_tps
        stream.updated_at = now
        stream.save(update_fields=["target_tps", "updated_at"])
        _audit_lifecycle(
            "streams.stream.target_tps_changed",
            stream,
            actor,
            extra={"target_tps": target_tps, "previous_target_tps": previous},
        )
    return stream


def request_rename(*, stream: Stream, name: str, actor: Any) -> Stream:
    """PATCH the stream ``name`` (a non-pinned label; api-spec §4.8.2). Idempotent.

    ``name`` is a free-form label, not part of the determinism pin (PIN-4), so it is
    mutable at any lifecycle state. Idempotent: setting the current name is a no-op.
    """
    if stream.name == name:
        return stream
    now = timezone.now()
    with transaction.atomic():
        stream.name = name
        stream.updated_at = now
        stream.save(update_fields=["name", "updated_at"])
        _audit_lifecycle("streams.stream.renamed", stream, actor, extra={"name": name})
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
