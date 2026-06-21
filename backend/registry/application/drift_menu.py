"""The drift field menu — per-business-subject next-version snapshot (§11, DR-1).

The single application seam that turns a stream's **effective version map** into
the ``registry_view`` snapshot the chaos ``schema_drift`` stage reads (chaos-engine
§5.5). The effective map itself (``effective = max(materialized pin, highest applied
upgrade)``, §10.2) is owned by :mod:`streams.application.schema_pins`; this module
takes that map as input and resolves each business subject's next registered version
plus the fields it adds. Two callers share it:

* :func:`build_drift_menu` is what :mod:`streams.application.desired_state` embeds in
  the runner's desired-state document each poll (EM-5): the runner hands it to the
  pure engine as the ``registry_view`` port (DR-1). Because the menu keys off the
  **current effective version** the caller passes in, an applied mid-stream upgrade
  (which raises the effective version, §10.4 step 4) automatically drops the
  now-effective version's fields from the next refresh — DR-4 needs no extra step.
* :func:`drift_arming_eligible` is the config-time CH-V07 check (DR-3): enabling
  ``schema_drift`` on a stream where *no* business subject has a registered version
  above its effective version is rejected (chaos-engine §3.4). Per-subject
  ineligibility with the mode otherwise armed is not an error — ineligible subjects
  simply never produce a menu entry.

Per §11 the injection target is always the **next** registered version (the lowest
strictly greater than effective — "next, not latest": one evolution step at a time),
bounded by a configurable ceiling (default ``effective + 1``). The added-field set is
the top-level properties the next version introduces over the effective version, each
carried as ``{path, fragment}`` — ``path`` the flat property name the engine writes
into the payload, ``fragment`` the raw JSON-Schema sub-document drift synthesizes a
value from (DR-2). CDC subjects are never eligible (drift never licenses CDC
evolution — §10 REG-U006 / R-CDC-6); the caller filters them out by passing only the
business slice of the effective map, and :func:`build_drift_menu` re-checks via the
registry derivation as defense in depth.

Read-only; reuses the registry read managers and :func:`registry.infra.derive.
derive_subjects` (the authoritative business/CDC split). No write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from registry.application.services import _resolve_subject, _versions_for

__all__ = [
    "DEFAULT_CEILING_STEP",
    "DriftMenuEntry",
    "build_drift_menu",
    "drift_arming_eligible",
]

# Default ceiling: drift targets exactly the next version (effective + 1). A larger
# step would let drift reach past the immediate next registered version; the §11 rule
# is "next, not latest", so the default keeps the teaching to one step.
DEFAULT_CEILING_STEP = 1


@dataclass(frozen=True)
class DriftMenuEntry:
    """One business subject's drift menu (DR-1 / EM-5).

    ``effective_version`` is the stream's effective version for the subject;
    ``next_version`` the lowest registered version strictly above it (within the
    ceiling); ``next_added_fields`` the top-level properties the next version adds,
    each ``{"path": name, "fragment": sub-document}`` — the engine's
    :class:`~dataforge_engine.chaos.stages.schema_drift.DriftMenu` shape.

    The engine ``DriftMenu`` protocol reads ``from_version`` / ``to_version`` /
    ``added_fields`` directly, so this value object IS the port's menu — no adapter
    is needed between the application snapshot and the engine (the runner wraps a
    ``{subject: DriftMenuEntry}`` dict in a trivial ``menu_for`` provider).
    """

    effective_version: int
    next_version: int
    next_added_fields: list[dict[str, Any]]

    @property
    def from_version(self) -> int:
        return self.effective_version

    @property
    def to_version(self) -> int:
        return self.next_version

    @property
    def added_fields(self) -> list[dict[str, Any]]:
        return self.next_added_fields

    def to_dict(self) -> dict[str, Any]:
        """The desired-state document shape (EM-5): a JSON-serializable snapshot."""
        return {
            "effective_version": self.effective_version,
            "next_version": self.next_version,
            "next_added_fields": [dict(f) for f in self.next_added_fields],
        }


def build_drift_menu(
    *,
    effective: dict[str, int],
    workspace_id: UUID | None,
    ceiling_step: int = DEFAULT_CEILING_STEP,
) -> dict[str, DriftMenuEntry]:
    """The per-business-subject drift menu (DR-1 / EM-5), keyed on EFFECTIVE version.

    ``effective`` is the §10.2 ``{subject: effective_version}`` map (from
    :func:`streams.application.schema_pins.effective_versions`). For each *business*
    subject in it, find the next registered version strictly above its effective
    version (and ≤ ``effective + ceiling_step``); when one exists, the entry carries
    the top-level fields it adds over the effective version document, as
    ``{path, fragment}`` (DR-2 synthesis input). CDC subjects and subjects with no
    registered next version within the ceiling are omitted — ineligible, not an error
    (DR-3). Because the effective version is the key, applying an upgrade (which raises
    the value passed in) drops the menu entry on the next refresh (DR-4).
    """
    step = max(ceiling_step, 1)
    menu: dict[str, DriftMenuEntry] = {}
    for subject_name, effective_version in effective.items():
        if _is_cdc_subject(subject_name):
            continue  # drift never targets CDC subjects (§10 REG-U006 / R-CDC-6)
        subject = _resolve_subject(subject_name, workspace_id)
        if subject is None:
            continue
        by_version = {v.version: v for v in _versions_for(subject)}
        if effective_version not in by_version:
            continue  # effective doc must exist to diff against (gapless by INV-REG-2)
        ceiling = effective_version + step
        candidates = sorted(n for n in by_version if effective_version < n <= ceiling)
        if not candidates:
            continue  # no registered next version within the ceiling ⇒ ineligible
        next_version = candidates[0]
        added = _added_top_level_fields(
            from_document=dict(by_version[effective_version].json_schema),
            to_document=dict(by_version[next_version].json_schema),
        )
        if not added:  # a next version that adds no top-level field is nothing to drift
            continue
        menu[subject_name] = DriftMenuEntry(
            effective_version=effective_version,
            next_version=next_version,
            next_added_fields=added,
        )
    return menu


def drift_arming_eligible(
    *,
    effective: dict[str, int],
    workspace_id: UUID | None,
    ceiling_step: int = DEFAULT_CEILING_STEP,
) -> bool:
    """CH-V07 (DR-3): at least one business subject has a registered next version.

    True when :func:`build_drift_menu` would yield ≥ 1 entry — i.e. ``schema_drift``
    can draw a field from somewhere. False means the mode would be a structural no-op
    for every subject and the API rejects enabling it (chaos-engine §3.4).
    """
    return bool(
        build_drift_menu(
            effective=effective, workspace_id=workspace_id, ceiling_step=ceiling_step
        )
    )


def _is_cdc_subject(subject_name: str) -> bool:
    """A CDC subject is ``{slug}.cdc.{entity}`` (INV-REG-1); business is ``{slug}.{event}``."""
    return subject_name.split(".", 2)[1:2] == ["cdc"]


def _added_top_level_fields(
    *, from_document: dict[str, Any], to_document: dict[str, Any]
) -> list[dict[str, Any]]:
    """Top-level properties of ``to`` absent from ``from``, as ``{path, fragment}``.

    Drift adds fields at the payload top level (``target[path] = value`` in the engine
    stage), so the menu carries the flat property name and its raw fragment — distinct
    from the diff API's JSON-Pointer ``{path, type, required}`` shape
    (:mod:`registry.infra.diff`), which is the consumer-facing wire diff. Both are
    additive-only by INV-REG-3; this keeps the fragment intact for type-directed
    synthesis (the ``type``/``pattern``/``enum``/``const`` the engine reads, DR-2).
    """
    from_props: dict[str, Any] = from_document.get("properties", {}) or {}
    to_props: dict[str, Any] = to_document.get("properties", {}) or {}
    added: list[dict[str, Any]] = []
    for name, fragment in to_props.items():
        if name not in from_props:
            added.append({"path": name, "fragment": dict(fragment)})
    return added
