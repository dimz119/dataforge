"""Pre-warmed scheduled-upgrade cutovers for the runner (schema-registry §10.4, EM-5).

The companion to :mod:`registry.application.drift_menu`: where that turns a stream's
effective map into the drift field menu, this turns the stream's effective map + its
``schema_upgrade_schedule`` into the **pre-warmed cutovers** the runner applies at the
virtual-clock boundary. Both ride the desired-state document the runner polls each tick
(EM-5 — "the data plane never blocks on a registry query mid-tick"); both reuse
:mod:`streams.application.schema_pins` for the §10.2 effective version and the registry
read under ``platform_read_scope``.

For each business subject with a ``scheduled`` upgrade whose ``target_version`` is above
the subject's effective version, this pre-warms the target chain into a
:class:`~dataforge_engine.behavior.ir.SchemaCutover`: the compiled union of added-field
bindings across the versions in ``(effective, target]`` (§10.3, version-skipping), plus
the schedule ``at`` (the §10.4 cutover gate). The runner hands the resulting
``{event_type: SchemaCutover}`` map to the pure engine via
``compile_manifest_cached(schema_cutovers=)``; the per-event ``occurred_at`` gate inside
the interpreter does the actual atomic-between-events switch (§10.4 step 3). CDC subjects
never carry a cutover (REG-U006).

The ``at`` is converted from its absolute RFC-3339 instant into the engine's *offset*
domain (simulated µs since ``virtual_epoch``) — the domain ``VirtualClock.virtual_now_us``
and the interpreter's ``occurred_at_us`` gate use. A schedule entry with no ``at`` is a
"next tick" cutover (§10.3): its gate is the current virtual ``now``, so it fires this
tick. Read-only; no write — the runner persists applied state into its checkpoint.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.behavior.ir import SchemaCutover
from registry.infra.upgrade_plan import build_added_field_bindings

__all__ = ["pre_warm_cutovers"]


def pre_warm_cutovers(
    *,
    manifest: dict[str, Any],
    effective: dict[str, int],
    schedule: list[dict[str, Any]] | None,
    virtual_epoch_ms: int,
    virtual_now_offset_us: int,
) -> dict[str, SchemaCutover]:
    """Pre-warm every armed ``scheduled`` upgrade into an engine cutover (§10.4 step 1).

    ``effective`` is the §10.2 ``{subject: version}`` map (from
    :func:`streams.application.schema_pins.effective_versions`); ``schedule`` the
    persisted ``schema_upgrade_schedule`` list. For each ``scheduled`` entry whose
    ``target_version`` exceeds the subject's effective version, build the union chain of
    added-field bindings (versions ``(effective, target]``) and the ``at`` gate, keyed by
    the subject's **event type** (the engine indexes ``schema_cutovers`` by event type).

    One batched registry read under ``platform_read_scope`` (the runner spans
    workspaces, §8.3). Returns ``{}`` when nothing is armed — the steady-state path,
    where the runner compiles the un-extended IR (cache hit).
    """
    scheduled = _scheduled_entries(schedule)
    if not scheduled:
        return {}

    from registry.application.services import _resolve_subject, _versions_for
    from streams.application.schema_pins import subject_to_event_type
    from tenancy.application.services import platform_read_scope

    event_types = subject_to_event_type(manifest)
    cutovers: dict[str, SchemaCutover] = {}
    with platform_read_scope():
        for subject_name, entry in scheduled.items():
            event_type = event_types.get(subject_name)
            if event_type is None:
                continue  # not a subject this manifest emits (REG-U001 guards create)
            cutover = _cutover_for(
                subject_name=subject_name,
                entry=entry,
                effective=int(effective.get(subject_name, 0)),
                virtual_epoch_ms=virtual_epoch_ms,
                virtual_now_offset_us=virtual_now_offset_us,
                resolve_subject=_resolve_subject,
                versions_for=_versions_for,
            )
            if cutover is not None:
                cutovers[event_type] = cutover
    return cutovers


def _cutover_for(
    *,
    subject_name: str,
    entry: dict[str, Any],
    effective: int,
    virtual_epoch_ms: int,
    virtual_now_offset_us: int,
    resolve_subject: Any,
    versions_for: Any,
) -> SchemaCutover | None:
    """Build one subject's :class:`SchemaCutover` from its scheduled entry."""
    target = _coerce_int(entry.get("target_version"))
    if target is None or target <= effective:
        return None  # already at/above target (REG-U003 guards create; defensive here)
    subject = resolve_subject(subject_name, None)
    if subject is None:
        return None
    by_version = {v.version: dict(v.json_schema) for v in versions_for(subject)}
    if effective not in by_version or target not in by_version:
        return None  # chain incomplete — cannot pre-warm
    # The chain documents: the effective doc (the diff root) followed by every version
    # in (effective, target] ascending — the union of added fields (§10.3, "1 → 3 = the
    # union of versions 2 and 3"). INV-REG-2 guarantees a gapless chain.
    chain = [by_version[v] for v in range(effective, target + 1) if v in by_version]
    added_bindings = build_added_field_bindings(chain)
    at_offset_us = _at_offset_us(
        entry.get("at"),
        virtual_epoch_ms=virtual_epoch_ms,
        virtual_now_offset_us=virtual_now_offset_us,
    )
    return SchemaCutover(
        at_us=at_offset_us,
        target_version=target,
        added_bindings=added_bindings,
    )


def _scheduled_entries(schedule: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Subject → its single ``scheduled`` entry (REG-U007: ≤ 1 pending per subject)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in schedule or []:
        if isinstance(entry, dict) and entry.get("status") == "scheduled":
            out[str(entry.get("subject"))] = entry
    return out


def _at_offset_us(
    at_value: Any, *, virtual_epoch_ms: int, virtual_now_offset_us: int
) -> int:
    """An ISO-8601 simulated ``at`` → offset µs since ``virtual_epoch`` (§10.4 gate).

    The schedule stores ``at`` as the §10.3 RFC-3339 microsecond instant (absolute,
    ``occurred_at`` domain). The interpreter gate (``occurred_at_us``) and the clock
    (``virtual_now_us``) are in the **offset** domain (simulated µs since
    ``virtual_epoch``), so rebase: ``at_offset = at_epoch_us - virtual_epoch_ms x 1000``.
    A missing/empty ``at`` means "the next tick boundary" (§10.3) — return the current
    virtual ``now`` offset so every event this tick (``occurred_at ≥ now``) is on/after
    the cutover and it fires immediately.
    """
    if at_value in (None, ""):
        return virtual_now_offset_us
    from datetime import datetime

    text = str(at_value).replace("Z", "+00:00")
    at_epoch_us = int(datetime.fromisoformat(text).timestamp() * 1_000_000)
    return at_epoch_us - virtual_epoch_ms * 1000


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
