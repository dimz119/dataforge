"""Per-stream schema-version pins + the effective-version projection (schema-registry §10.1-10.2).

The stream column ``schema_version_pins`` (jsonb ``{subject: version}``, default
``{}``) is part of the determinism pin (INV-STR-5): set at create, immutable once
first started. This module owns the three pure-ish read services the rest of the
phase plumbs through:

* :func:`validate_pins` (PIN-R3) — the **create-time** gate: every key must be a
  subject the stream's pinned manifest emits, every value a registered version of
  it. Violations raise :class:`config.problems.PinValidationFailed` (422) with the
  ``errors[] {code, path, message}`` extension naming each bad entry.
* :func:`materialize_pins` (PIN-R1/R2) — the **first-start** resolution: for every
  subject the pinned manifest emits, the latest registered version *at that moment*
  (PIN-R1, latest-resolved-once), with explicit ``schema_version_pins`` entries
  overriding per subject (PIN-R2). The result is the materialized pin map written to
  the first checkpoint; restarts continue it unchanged.
* :func:`effective_versions` / :func:`schema_versions_view` — the §10.2 effective
  map ``effective_version(stream, subject) = max(materialized pin, highest applied
  upgrade target)`` and the ``{effective, pending, applied}`` projection the
  ``GET /streams/{id}/schema-versions`` endpoint and the additive ``schema_versions``
  Stream-resource field serve.

Subject → version is registry-owned; this module reaches it through the registry's
``subjects_emitted_with_latest`` seam (the same derivation + latest-resolution used
for validation, §10.1) and ``get_version`` for the existence checks. The registry is
read under ``platform_read_scope`` so a global/builtin scenario's NULL-workspace
subjects are visible regardless of the armed workspace (Flow 2 registers globals,
§5.2). No write happens here — materialization is persisted by the runner into the
checkpoint, the upgrade workstream persists applied entries.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

__all__ = [
    "effective_versions",
    "effective_versions_for_stream",
    "engine_schema_versions",
    "materialize_pins",
    "schema_versions_view",
    "schema_versions_view_for_stream",
    "subject_to_event_type",
    "validate_pins",
]


def _emitted_latest(manifest: dict[str, Any]) -> dict[str, int | None]:
    """Every subject ``manifest`` emits → its latest registered version (or ``None``).

    The registry's §10.1 materialization seam, read global-visible (Flow 2 registers
    NULL-workspace subjects for the builtin scenario, §5.2): under
    ``platform_read_scope`` the strict registry RLS admits global rows to the runtime
    role for this read-only resolution. ``workspace_id=None`` resolves globals only,
    which is correct for the builtin scenarios this phase ships; a future
    workspace-owned scenario would pass its workspace through here.
    """
    from registry.application.services import subjects_emitted_with_latest
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        return subjects_emitted_with_latest(manifest, workspace_id=None)


def _subject_event_types(manifest: dict[str, Any]) -> dict[str, str]:
    """Map ``{subject: event_type}`` for every subject the manifest emits.

    The engine keys its ``schema_versions`` override by *event type* (business
    ``order_placed`` / CDC ``cdc.users``), not by the dotted subject name. The
    registry derivation is the authoritative enumeration of both (R-DER-1..3).
    """
    from registry.infra.derive import derive_subjects

    return {d.subject: d.event_type for d in derive_subjects(manifest)}


def validate_pins(pins: dict[str, Any], *, manifest: dict[str, Any]) -> None:
    """PIN-R3 create-time validation; raise ``PinValidationFailed`` (422) on any bad entry.

    Every key must be a subject the stream's pinned manifest emits (business or CDC,
    PIN-R4 — CDC subjects are pinnable), and every value a registered version of that
    subject. Pinning above latest is impossible (the version must exist); pinning
    below latest is the point (PIN-R2, the evolution exercise). An empty map is valid
    (the PIN-R1 latest-at-first-start default). Each violation contributes one
    ``errors[]`` entry; the whole list is reported at once.
    """
    if not pins:
        return
    from config.problems import PinValidationFailed
    from registry.application.services import get_version
    from tenancy.application.services import platform_read_scope

    emitted = _emitted_latest(manifest)
    errors: list[dict[str, Any]] = []
    with platform_read_scope():
        for subject, raw_version in pins.items():
            path = f"/schema_version_pins/{subject}"
            if subject not in emitted:
                errors.append(
                    {
                        "code": "PIN-R3",
                        "path": path,
                        "message": (
                            f"{subject!r} is not a subject this stream's scenario emits."
                        ),
                    }
                )
                continue
            version = _coerce_version(raw_version)
            if version is None:
                errors.append(
                    {
                        "code": "PIN-R3",
                        "path": path,
                        "message": f"pin value {raw_version!r} is not a positive integer version.",
                    }
                )
                continue
            registered = get_version(subject, version, workspace_id=None)
            if registered is None:
                errors.append(
                    {
                        "code": "PIN-R3",
                        "path": path,
                        "message": (
                            f"version {version} is not a registered version of {subject!r}."
                        ),
                    }
                )
    if errors:
        raise PinValidationFailed(errors=errors)


def materialize_pins(
    explicit_pins: dict[str, Any], *, manifest: dict[str, Any]
) -> dict[str, int]:
    """PIN-R1/R2: resolve the materialized ``{subject: version}`` map at first start.

    For every subject the pinned manifest emits, the latest registered version *at
    this moment* (PIN-R1 — including Flow 2 evolutions; resolved exactly once per
    stream, then carried unchanged in the checkpoint). An explicit
    ``schema_version_pins`` entry overrides per subject (PIN-R2). A subject the
    manifest declares that has no registered version yet is omitted (the manifest was
    never published — there is nothing to pin); validation already ran at create, so
    explicit entries here are known-good.

    Pure read; the caller (the runner first-start path) persists the result into the
    first checkpoint as the materialized pin.
    """
    emitted = _emitted_latest(manifest)
    materialized: dict[str, int] = {}
    for subject, latest in emitted.items():
        explicit = _coerce_version(explicit_pins.get(subject)) if explicit_pins else None
        if explicit is not None:
            materialized[subject] = explicit
        elif latest is not None:
            materialized[subject] = latest
    return materialized


def effective_versions(
    materialized: dict[str, Any], applied: dict[str, Any] | None = None
) -> dict[str, int]:
    """§10.2 ``effective = max(materialized pin, highest applied upgrade target)`` per subject.

    ``materialized`` is the PIN-R1/R2 map carried in the checkpoint; ``applied`` is
    the highest applied upgrade target per subject (the upgrade workstream's output,
    also checkpoint-persisted). Subjects present only in ``applied`` (an upgrade
    target for a subject not in the materialized map — should not happen, REG-U001
    guards it) still surface so the projection is total.
    """
    applied = applied or {}
    subjects = set(materialized) | set(applied)
    effective: dict[str, int] = {}
    for subject in subjects:
        pinned = _coerce_version(materialized.get(subject)) or 0
        upgraded = _coerce_version(applied.get(subject)) or 0
        effective[subject] = max(pinned, upgraded)
    return {s: v for s, v in effective.items() if v > 0}


def engine_schema_versions(
    effective: dict[str, int], *, manifest: dict[str, Any]
) -> dict[str, int]:
    """Re-key the effective ``{subject: version}`` map to the engine's ``{event_type: version}``.

    ``compile_manifest_cached(schema_versions=)`` keys by event type (§5.3); the
    effective map keys by dotted subject. The registry derivation supplies the
    subject→event_type map. Only entries above v1 need an override (v1 is the
    manifest-derived default), but passing the full map is harmless and explicit.
    """
    event_types = _subject_event_types(manifest)
    out: dict[str, int] = {}
    for subject, version in effective.items():
        event_type = event_types.get(subject)
        if event_type is not None:
            out[event_type] = int(version)
    return out


def subject_to_event_type(manifest: dict[str, Any]) -> dict[str, str]:
    """Public accessor for the ``{subject: event_type}`` map (the cutover workstream's seam)."""
    return _subject_event_types(manifest)


def schema_versions_view(
    *,
    materialized: dict[str, Any],
    schedule: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The ``{effective, pending, applied}`` projection (GET /streams/{id}/schema-versions, §10.2).

    ``materialized`` is the checkpoint's materialized pin map (``{}`` before first
    start). ``schedule`` is the stream's ``schema_upgrade_schedule`` document — the
    upgrade workstream's persisted entry list, each ``{upgrade_id, subject,
    target_version, at, status, ...}``; ``applied`` entries additionally carry
    ``applied_at_wall`` and ``applied_sequence_no`` (§10.3). ``effective`` folds the
    materialized pin with the highest applied target per subject (§10.2). ``pending``
    is the ``scheduled`` entries; ``applied`` the ``applied`` entries (cancelled
    entries are history, surfaced only on the upgrade-list endpoint).
    """
    entries = list(schedule or [])
    pending = [e for e in entries if e.get("status") == "scheduled"]
    applied_entries = [e for e in entries if e.get("status") == "applied"]
    applied_targets: dict[str, int] = {}
    for entry in applied_entries:
        subject = str(entry.get("subject"))
        target = _coerce_version(entry.get("target_version")) or 0
        applied_targets[subject] = max(applied_targets.get(subject, 0), target)
    effective = effective_versions(materialized, applied_targets)
    return {
        "effective": effective,
        "pending": pending,
        "applied": applied_entries,
    }


def effective_versions_for_stream(stream: Any) -> dict[str, int]:
    """The §10.2 effective ``{subject: version}`` map for a live or created ``stream``.

    The single source the additive ``schema_versions`` Stream-resource field and the
    ``GET /streams/{id}/schema-versions`` endpoint share. Sourcing rule:

    * **After first start** (a checkpoint exists): the materialized pin from the
      checkpoint (resolved exactly once at T1→T3, PIN-R1) folded with the highest
      applied upgrade target per subject — both checkpoint-persisted, the §10.2
      contract that survives pause/restart/failover.
    * **Before first start** (no checkpoint yet, a ``created`` stream): a *preview* of
      what materialization will pick — the explicit ``schema_version_pins`` entries
      over each subject's latest registered version (PIN-R1/R2 applied to the pinned
      manifest). This makes a created-but-not-started stream surface a meaningful
      effective map; the runner re-resolves authoritatively (and freezes it) at start.

    Applied upgrade targets are read from the persisted ``schema_upgrade_schedule``
    (the control-plane truth the runner also writes back) and folded in either case.
    """
    materialized = materialized_from_checkpoint(stream.id)
    applied_checkpoint = applied_from_checkpoint(stream.id)
    schedule_applied = _applied_from_schedule(stream.schema_upgrade_schedule)
    applied = {**schedule_applied, **applied_checkpoint}
    if not materialized:
        # Before first start: preview materialization from the pinned manifest.
        materialized = materialize_pins(
            dict(stream.schema_version_pins or {}), manifest=dict(stream.pinned_config or {})
        )
    return effective_versions(materialized, applied)


def schema_versions_view_for_stream(stream: Any) -> dict[str, Any]:
    """``{effective, pending, applied}`` for ``stream`` (GET /streams/{id}/schema-versions).

    ``effective`` is :func:`effective_versions_for_stream`; ``pending``/``applied`` are
    the ``scheduled``/``applied`` entries of the persisted ``schema_upgrade_schedule``
    (cancelled entries are history, surfaced only on the upgrade-list endpoint).
    """
    entries = [e for e in (stream.schema_upgrade_schedule or []) if isinstance(e, dict)]
    return {
        "effective": effective_versions_for_stream(stream),
        "pending": [e for e in entries if e.get("status") == "scheduled"],
        "applied": [e for e in entries if e.get("status") == "applied"],
    }


def _applied_from_schedule(schedule: Any) -> dict[str, int]:
    """Highest applied upgrade target per subject from the persisted schedule list."""
    applied: dict[str, int] = {}
    for entry in schedule or []:
        if not isinstance(entry, dict) or entry.get("status") != "applied":
            continue
        subject = str(entry.get("subject"))
        target = _coerce_version(entry.get("target_version")) or 0
        if target > applied.get(subject, 0):
            applied[subject] = target
    return applied


def materialized_from_checkpoint(stream_id: UUID | str, shard_id: int = 0) -> dict[str, int]:
    """Load the materialized pin map persisted in the (stream, shard) checkpoint blob.

    The checkpoint blob carries ``schema_pins`` (the materialized PIN-R1/R2 map) and
    ``applied_upgrades`` (highest applied target per subject) under the ``runtime``
    key the runner writes (see :mod:`runner.checkpoint_store`). Before the first
    checkpoint exists this returns ``{}`` (the empty effective map a never-started or
    just-created stream surfaces). Read-only; runs under ``platform_read_scope`` so
    the control-plane endpoint (which resolves the row by unique id) can read the
    Class-T checkpoint without arming a workspace first.
    """
    return _coerce_version_map(_load_runtime(stream_id, shard_id).get("schema_pins"))


def applied_from_checkpoint(stream_id: UUID | str, shard_id: int = 0) -> dict[str, int]:
    """Load the highest-applied-upgrade-target map persisted in the checkpoint blob."""
    return _coerce_version_map(_load_runtime(stream_id, shard_id).get("applied_upgrades"))


def _coerce_version_map(raw: Any) -> dict[str, int]:
    """Coerce a persisted ``{subject: version}`` blob to a typed map (skip bad entries)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        version = _coerce_version(value)
        if version is not None:
            out[str(key)] = version
    return out


def _load_runtime(stream_id: UUID | str, shard_id: int) -> dict[str, Any]:
    """Decode the ``runtime`` sub-document of the (stream, shard) checkpoint blob.

    The checkpoint ``state`` is the zstd-compressed canonical-JSON engine blob; the
    runner nests the schema-evolution runtime state under ``runtime`` (a side-car the
    engine codec ignores). Returns ``{}`` when no checkpoint exists yet.
    """
    import json

    from generation.domain.models import StreamCheckpoint
    from generation.infra.compression import decompress
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        row: Any = StreamCheckpoint.all_objects.filter(
            stream_id=str(stream_id), shard_id=shard_id
        ).first()
    if row is None:
        return {}
    blob = json.loads(decompress(bytes(row.state)).decode("utf-8"))
    runtime = blob.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _coerce_version(raw: Any) -> int | None:
    """Coerce a pin/version value to a positive int, or ``None`` if it is not one."""
    if isinstance(raw, bool):  # bool is an int subclass; reject it explicitly
        return None
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return None
    return version if version >= 1 else None
