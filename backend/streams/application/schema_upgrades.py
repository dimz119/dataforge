"""Scheduled mid-stream schema-upgrade use cases (schema-registry §10.3, api-spec §4.8.4).

A learner schedules an *announced* additive evolution on a live stream: "evolve
``<scenario>.<event>`` to v2 at simulated time T". The control plane validates
the request (REG-U001..U007) and persists a ``scheduled`` entry into the stream's
``schema_upgrade_schedule`` jsonb column — the entry rides the desired-state document
the runner polls each tick (``streams.application.desired_state``); the runner cuts
the subject's effective version over at the first event whose ``occurred_at ≥ at``
(§10.4) and writes the ``applied`` transition back into the same entry.

This module owns three use cases — :func:`schedule_upgrade`, :func:`list_upgrades`,
:func:`cancel_upgrade` — plus the REG-U001..U007 validator (:func:`validate_upgrade`),
which returns *every* failure so the 409 ``conflict`` problem can list them all.

The persisted entry shape (the jsonb element the runner reads + completes):

.. code-block:: python

    {
        "upgrade_id": "<uuid>",        # stable id (URL segment, idempotency anchor)
        "subject": "<scenario>.<event>",
        "target_version": 2,
        "at": "2026-06-12T00:00:00.000000Z" | None,  # SIMULATED time; None ⇒ next tick
        "status": "scheduled" | "applied" | "cancelled",
        "created_at": "<rfc3339>",     # wall clock at scheduling
        "idempotency_key": "<str>" | None,  # I-1 replay anchor (omitted ⇒ None)
        # --- written by the runner cutover (§10.4 step 4), absent until applied: ---
        "applied_at_wall": "<rfc3339>" | None,   # wall instant the cutover fired
        "applied_sequence_no": <int> | None,     # first post-cutover seq per shard
        # --- written by cancel (retained in the list — irreversible history): ---
        "cancelled_at": "<rfc3339>" | None,
    }

The validation context — the stream's *pinned* manifest (``stream.pinned_config``),
its current *virtual time*, and the subject's currently *effective* version — is read
once and threaded through the catalog so each check is a pure predicate over it.

Application layer: owns the transaction boundary, calls the registry read services +
the pure ``registry.infra`` binding/derivation seams, and audits in-band (INV-AUD-2).
The runner host owns no model imports beyond the desired-state seam, so the schedule
travels to it as plain jsonb (no ORM rows escape).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from django.db import transaction
from django.utils import timezone

from streams.application import audit
from streams.domain.models import Stream

__all__ = [
    "STATUS_APPLIED",
    "STATUS_CANCELLED",
    "STATUS_SCHEDULED",
    "ScheduleResult",
    "UpgradeError",
    "UpgradeNotCancellable",
    "UpgradeNotFound",
    "UpgradeValidationFailed",
    "cancel_upgrade",
    "effective_version_for",
    "list_upgrades",
    "mark_upgrade_applied",
    "schedule_upgrade",
    "validate_upgrade",
]

# The §10.3 entry lifecycle statuses (the jsonb ``status`` enum).
STATUS_SCHEDULED = "scheduled"
STATUS_APPLIED = "applied"
STATUS_CANCELLED = "cancelled"

# A cdc.* subject is rejected (REG-U006) — its segment after the slug is ``cdc``.
_CDC_MARKER = "cdc"


@dataclass(frozen=True)
class UpgradeError:
    """One REG-U001..U007 failure — the ``errors[]`` element of the 409 (§10.3)."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class UpgradeValidationFailed(Exception):
    """One or more REG-U001..U007 checks failed (→ 409 ``conflict`` with ``errors[]``)."""

    def __init__(self, errors: list[UpgradeError]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} upgrade validation failure(s)")


class UpgradeNotFound(Exception):
    """No upgrade with the given id on the stream (→ 404)."""


class UpgradeNotCancellable(Exception):
    """The upgrade is not in ``scheduled`` state (→ 409 ``invalid-state-transition``)."""


@dataclass(frozen=True)
class ScheduleResult:
    """The outcome of :func:`schedule_upgrade` — the persisted entry + replay flag."""

    entry: dict[str, Any]
    idempotent: bool = field(default=False)  # True ⇒ an Idempotency-Key replay no-op


# --- effective-version computation -----------------------------------------
#
# A subject's *effective* version on a stream is the max of its pin (the
# ``schema_version_pins`` map — empty ⇒ the subject's latest registered version,
# the PIN-R1 materialization default) and the highest ``applied`` upgrade target
# for that subject. REG-U003 forbids a target ≤ this; REG-U005 validates the chain
# ``(effective, target]``. (P10-03 surfaces this through GET /schema-versions; the
# computation lives here so the upgrade validator does not depend on that endpoint.)


def effective_version_for(
    stream: Stream, subject_name: str, *, latest_version: int | None
) -> int | None:
    """The subject's current effective version on ``stream`` (§10.1/§10.4).

    ``latest_version`` is the subject's latest registered version (the pin default
    when the pin map has no explicit entry). Returns ``None`` only when the subject
    has no registered version at all (the caller treats that as "no effective pin").
    """
    pins = dict(stream.schema_version_pins or {})
    pinned = pins.get(subject_name)
    base = int(pinned) if pinned is not None else latest_version
    applied_max = _highest_applied(stream, subject_name)
    candidates = [v for v in (base, applied_max) if v is not None]
    return max(candidates) if candidates else None


def _highest_applied(stream: Stream, subject_name: str) -> int | None:
    highest: int | None = None
    for entry in _entries(stream):
        if entry.get("subject") != subject_name or entry.get("status") != STATUS_APPLIED:
            continue
        target = int(entry.get("target_version", 0))
        if highest is None or target > highest:
            highest = target
    return highest


def _entries(stream: Stream) -> list[dict[str, Any]]:
    raw = stream.schema_upgrade_schedule
    return [e for e in (raw or []) if isinstance(e, dict)]


def _has_scheduled_for(stream: Stream, subject_name: str) -> bool:
    return any(
        e.get("subject") == subject_name and e.get("status") == STATUS_SCHEDULED
        for e in _entries(stream)
    )


# --- validation (REG-U001..U007) -------------------------------------------


def validate_upgrade(
    *, stream: Stream, subject_name: str, target_version: int, at: datetime | None
) -> list[UpgradeError]:
    """Run every REG-U001..U007 check; return all failures (empty ⇒ accept, §10.3).

    The pinned manifest (``stream.pinned_config``) is the resolution context for
    REG-U001 (subjects the pin emits) and REG-U005 (bindings resolving against the
    *pinned* — not the latest — manifest). Returns failures in catalog order; the
    caller raises :class:`UpgradeValidationFailed` carrying the list.
    """
    from registry.application.services import get_version
    from registry.infra.derive import derive_subjects

    errors: list[UpgradeError] = []
    workspace_id = stream.workspace_id
    manifest = dict(stream.pinned_config or {})

    # REG-U006: cdc.* subjects are never upgradable (synthesized row-image fields
    # would violate INV-GEN-6). Checked first — it makes every other check moot.
    if _is_cdc_subject(subject_name):
        errors.append(
            UpgradeError(
                "REG-U006",
                "/subject",
                f"'{subject_name}' is a cdc.* subject; CDC subjects cannot be "
                "upgraded (synthesized row-image fields would violate INV-GEN-6).",
            )
        )

    # REG-U001: the subject must be one the stream's pinned manifest emits.
    emitted = {d.subject for d in derive_subjects(manifest)}
    if subject_name not in emitted:
        errors.append(
            UpgradeError(
                "REG-U001",
                "/subject",
                f"'{subject_name}' is not emitted by the stream's pinned manifest "
                f"version ({stream.manifest_version}).",
            )
        )

    # REG-U002: target_version must be a registered version of the subject.
    target = get_version(subject_name, target_version, workspace_id=workspace_id)
    latest = get_version(subject_name, "latest", workspace_id=workspace_id)
    latest_version = latest.version if latest is not None else None
    if target is None:
        errors.append(
            UpgradeError(
                "REG-U002",
                "/target_version",
                f"version {target_version} is not a registered version of "
                f"'{subject_name}'.",
            )
        )

    # REG-U003: target_version must be strictly above the current effective version
    # (downgrades and re-application are impossible).
    effective = effective_version_for(stream, subject_name, latest_version=latest_version)
    if effective is not None and target_version <= effective:
        errors.append(
            UpgradeError(
                "REG-U003",
                "/target_version",
                f"target version {target_version} is not above the stream's current "
                f"effective version ({effective}); downgrades and re-application are "
                "impossible.",
            )
        )

    # REG-U004: an explicit ``at`` must be ≥ the stream's current virtual time.
    if at is not None:
        virtual_now = current_virtual_time(stream)
        if virtual_now is not None and at < virtual_now:
            errors.append(
                UpgradeError(
                    "REG-U004",
                    "/at",
                    "the upgrade 'at' is before the stream's current virtual time "
                    f"({_rfc3339(virtual_now)}); schedule it at or after now.",
                )
            )

    # REG-U005: every version in (effective, target] must carry bindings that resolve
    # against THIS stream's pinned manifest. Only checkable when the target exists and
    # an effective baseline is known (otherwise REG-U002/U003 already fired).
    if target is not None and effective is not None and target_version > effective:
        errors.extend(
            _validate_chain_bindings(
                subject_name=subject_name,
                from_version=effective,
                to_version=target_version,
                manifest=manifest,
                workspace_id=workspace_id,
            )
        )

    # REG-U007: at most one scheduled upgrade per subject per stream.
    if _has_scheduled_for(stream, subject_name):
        errors.append(
            UpgradeError(
                "REG-U007",
                "/subject",
                f"a scheduled upgrade already exists for '{subject_name}'; cancel it "
                "before scheduling another (one pending upgrade per subject).",
            )
        )

    return errors


def _validate_chain_bindings(
    *,
    subject_name: str,
    from_version: int,
    to_version: int,
    manifest: dict[str, Any],
    workspace_id: Any,
) -> list[UpgradeError]:
    """REG-U005: each step in ``(from, to]`` resolves against the *pinned* manifest.

    Reuses ``registry.infra.binding.check_added_bindings`` (the REG-C007 seam) per
    step — every property a version adds relative to its predecessor must carry a
    binding that resolves in the pinned manifest's emission context. A REG-C007
    failure here is reported as REG-U005 (the same shape, validated against *pinned*
    rather than *latest*). Version skipping (1 → 3) validates the whole chain.
    """
    from registry.application.services import get_versions_in_range
    from registry.infra.binding import check_added_bindings

    chain = get_versions_in_range(
        subject_name, from_version, to_version, workspace_id=workspace_id
    )
    if chain is None:
        return []  # a missing endpoint already surfaced as REG-U002

    errors: list[UpgradeError] = []
    for predecessor, candidate in pairwise(chain):
        compat_errors = check_added_bindings(
            latest=predecessor.json_schema,
            candidate=candidate.json_schema,
            manifest=manifest,
            subject=subject_name,
        )
        for compat in compat_errors:
            errors.append(
                UpgradeError(
                    "REG-U005",
                    compat.path,
                    f"version {candidate.version} binding does not resolve against "
                    f"this stream's pinned manifest ({manifest.get('version', '?')}): "
                    f"{compat.message}",
                )
            )
    return errors


def _is_cdc_subject(subject_name: str) -> bool:
    """A subject is CDC iff its segment after the scenario slug is ``cdc`` (INV-REG-1)."""
    parts = subject_name.split(".")
    return len(parts) >= 2 and parts[1] == _CDC_MARKER


# --- virtual-clock read -----------------------------------------------------


def current_virtual_time(stream: Stream) -> datetime | None:
    """The stream's current simulated time, or ``None`` if it cannot be computed yet.

    Live mode: ``virtual_epoch + speed_multiplier * (wall_now - first_started_at)``
    (behavior-engine §3.1; the same segment formula the runner's ``VirtualClock``
    uses, evaluated from the wall start anchor). A stream never started has no segment
    → ``None`` (REG-U004 is then vacuously satisfied — any future ``at`` is valid).
    Backfill mode has no live ``virtual_now`` → ``None``.
    """
    if stream.clock_mode != "live":
        return None
    anchor = stream.first_started_at
    if anchor is None:
        return None
    wall_delta_seconds = (timezone.now() - anchor).total_seconds()
    advanced = wall_delta_seconds * float(stream.speed_multiplier)
    from datetime import timedelta

    return stream.virtual_epoch + timedelta(seconds=advanced)


# --- use cases --------------------------------------------------------------


def schedule_upgrade(
    *,
    stream: Stream,
    subject_name: str,
    target_version: int,
    at: datetime | None,
    actor: Any,
    idempotency_key: str | None = None,
) -> ScheduleResult:
    """POST: validate + persist a ``scheduled`` entry (audit ``schema_upgrade_scheduled``).

    Validation runs first (REG-U001..U007); any failure raises
    :class:`UpgradeValidationFailed`. Idempotency-Key (I-1): a repeat with the same
    key returns the already-persisted entry unchanged (no second audit, no duplicate).
    The entry is appended to ``schema_upgrade_schedule`` so it rides the desired-state
    document the runner polls; ``at`` is stored as a SIMULATED instant (§10.3).
    """
    if idempotency_key:
        existing = _find_by_idempotency_key(stream, idempotency_key)
        if existing is not None:
            return ScheduleResult(entry=dict(existing), idempotent=True)

    errors = validate_upgrade(
        stream=stream,
        subject_name=subject_name,
        target_version=target_version,
        at=at,
    )
    if errors:
        raise UpgradeValidationFailed(errors)

    entry = _new_entry(
        subject_name=subject_name,
        target_version=target_version,
        at=at,
        idempotency_key=idempotency_key,
    )
    with transaction.atomic():
        # Re-read FOR UPDATE so concurrent schedules on the same stream serialize on
        # the row (the REG-U007 "one pending" guard is re-checked under the lock).
        locked = Stream.objects.select_for_update().get(id=stream.id)
        if idempotency_key:
            replay = _find_by_idempotency_key(locked, idempotency_key)
            if replay is not None:
                return ScheduleResult(entry=dict(replay), idempotent=True)
        if _has_scheduled_for(locked, subject_name):
            raise UpgradeValidationFailed(
                [
                    UpgradeError(
                        "REG-U007",
                        "/subject",
                        f"a scheduled upgrade already exists for '{subject_name}'; "
                        "cancel it before scheduling another.",
                    )
                ]
            )
        schedule = _entries(locked)
        schedule.append(entry)
        locked.schema_upgrade_schedule = schedule
        locked.updated_at = timezone.now()
        locked.save(update_fields=["schema_upgrade_schedule", "updated_at"])
        _audit(
            "streams.stream.schema_upgrade_scheduled",
            locked,
            actor,
            extra={
                "upgrade_id": entry["upgrade_id"],
                "subject": subject_name,
                "target_version": target_version,
                "at": entry["at"],
            },
        )
    return ScheduleResult(entry=entry, idempotent=False)


def list_upgrades(stream: Stream) -> list[dict[str, Any]]:
    """GET: every schedule entry (``scheduled``/``applied``/``cancelled``), newest last.

    Cancelled entries are retained (irreversible history is the audit posture, §10.3).
    Returned in insertion order (the order they were scheduled) — the API paginates.
    """
    return [dict(e) for e in _entries(stream)]


def cancel_upgrade(*, stream: Stream, upgrade_id: str, actor: Any) -> dict[str, Any]:
    """DELETE: cancel a ``scheduled`` entry (audit ``schema_upgrade_cancelled``).

    Only a ``scheduled`` entry may be cancelled → otherwise
    :class:`UpgradeNotCancellable` (409 ``invalid-state-transition``). The entry is
    retained with ``status = cancelled`` (irreversible history). An unknown id raises
    :class:`UpgradeNotFound` (404).
    """
    with transaction.atomic():
        locked = Stream.objects.select_for_update().get(id=stream.id)
        schedule = _entries(locked)
        index = next(
            (i for i, e in enumerate(schedule) if e.get("upgrade_id") == upgrade_id),
            None,
        )
        if index is None:
            raise UpgradeNotFound()
        entry = schedule[index]
        if entry.get("status") != STATUS_SCHEDULED:
            raise UpgradeNotCancellable()
        entry = dict(entry)
        entry["status"] = STATUS_CANCELLED
        entry["cancelled_at"] = _rfc3339(timezone.now())
        schedule[index] = entry
        locked.schema_upgrade_schedule = schedule
        locked.updated_at = timezone.now()
        locked.save(update_fields=["schema_upgrade_schedule", "updated_at"])
        _audit(
            "streams.stream.schema_upgrade_cancelled",
            locked,
            actor,
            extra={
                "upgrade_id": upgrade_id,
                "subject": entry.get("subject"),
                "target_version": entry.get("target_version"),
            },
        )
    return entry


def mark_upgrade_applied(
    *,
    stream_id: Any,
    upgrade_id: str,
    applied_at_wall: datetime,
    applied_sequence_no: int,
) -> dict[str, Any] | None:
    """Runner cutover bookkeeping (§10.4 step 4): flip a ``scheduled`` entry ``applied``.

    Called from the runner the tick the subject's first event crosses ``occurred_at ≥
    at``. Writes the ``applied`` transition + ``applied_at_wall`` (the wall instant the
    cutover fired) and the per-shard ``applied_sequence_no`` (the first post-cutover
    ``sequence_no``) back into the same jsonb entry, and audits
    ``streams.stream.schema_upgrade_applied`` (a *system* actor — the runner is a
    platform process with no request principal, §7.1). Idempotent: an entry already
    ``applied`` (a re-application after a restart whose ``at`` had already passed, §10.4
    "Stop/restart" / "failover") returns it unchanged with no second audit — the
    effective cutover is recorded in the checkpoint, so the schedule transition only
    needs to happen once. An unknown id or a ``cancelled`` entry returns ``None`` (the
    runner logs and moves on; cancellation raced the cutover).

    Runs in the caller's worker thread under the stream's armed workspace scope (the
    runner arms ``worker_workspace_scope`` before calling, like every other data-plane
    write); the ``select_for_update`` serializes against a concurrent cancel/schedule.
    """
    with transaction.atomic():
        locked = Stream.objects.select_for_update().filter(id=stream_id).first()
        if locked is None:
            return None
        schedule = _entries(locked)
        index = next(
            (i for i, e in enumerate(schedule) if e.get("upgrade_id") == upgrade_id),
            None,
        )
        if index is None:
            return None
        entry = dict(schedule[index])
        status = entry.get("status")
        if status == STATUS_APPLIED:
            return entry  # already recorded (restart/failover re-fire) — idempotent
        if status != STATUS_SCHEDULED:
            return None  # cancelled raced the cutover — nothing to apply
        entry["status"] = STATUS_APPLIED
        entry["applied_at_wall"] = _rfc3339(applied_at_wall)
        entry["applied_sequence_no"] = int(applied_sequence_no)
        schedule[index] = entry
        locked.schema_upgrade_schedule = schedule
        locked.updated_at = timezone.now()
        locked.save(update_fields=["schema_upgrade_schedule", "updated_at"])
        _audit(
            "streams.stream.schema_upgrade_applied",
            locked,
            None,  # system actor: the runner cutover, not a request principal (§7.1)
            extra={
                "upgrade_id": upgrade_id,
                "subject": entry.get("subject"),
                "target_version": entry.get("target_version"),
                "applied_at_wall": entry["applied_at_wall"],
                "applied_sequence_no": entry["applied_sequence_no"],
            },
        )
    return entry


# --- helpers ----------------------------------------------------------------


def _new_entry(
    *,
    subject_name: str,
    target_version: int,
    at: datetime | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Build a fresh ``scheduled`` jsonb entry (the runner completes it at cutover)."""
    return {
        "upgrade_id": str(uuid.uuid4()),
        "subject": subject_name,
        "target_version": target_version,
        "at": _rfc3339(at) if at is not None else None,
        "status": STATUS_SCHEDULED,
        "created_at": _rfc3339(timezone.now()),
        "idempotency_key": idempotency_key,
        # Completed by the runner cutover (§10.4 step 4); absent values until then.
        "applied_at_wall": None,
        "applied_sequence_no": None,
        "cancelled_at": None,
    }


def _find_by_idempotency_key(stream: Stream, key: str) -> dict[str, Any] | None:
    return next(
        (e for e in _entries(stream) if e.get("idempotency_key") == key),
        None,
    )


def _rfc3339(value: datetime) -> str:
    """Microsecond-precision RFC 3339 UTC string (the §10.3 wire format)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _audit(action: str, stream: Stream, actor: Any, *, extra: dict[str, Any]) -> None:
    audit.emit(
        action,
        actor=actor,
        workspace_id=stream.workspace_id,
        target={"type": "stream", "id": str(stream.id), "label": stream.name},
        metadata=extra,
    )
