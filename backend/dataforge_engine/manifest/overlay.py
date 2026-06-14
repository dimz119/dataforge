"""Workspace configuration overlay merge for re-validation (§11.1).

A ScenarioInstance holds an overlay (never a forked manifest); on every overlay
write the catalog re-validates the **merged** document (§11.1) so that probability
sums, V207 expected-steps, durations, and referential checks for
``cdc_entities``/``catalog_sizes`` keys all still hold. Errors reuse the MAN-V
codes with ``scope: "override"``.

This module produces the merged document the Layer-2 checks run against. The
overlay can only tune values the manifest already declares — it can never add
states, generators, entities, or payload fields — so Layer 1 and Layer 3 are not
re-run for overlays (§11.1).

Overlay shape (§11.1)::

    probabilities: { "machine.state.to": 0.55 }
    dwell:         { "machine.state.to": { family, … } }
    catalog_sizes: { users: 20000 }
    intensity:     { diurnal: [...], weekly: {...} }   # full replacement
    cdc_entities:  [users, products]                   # subset of manifest cdc.entities
    chaos:         { duplicates: { enabled: true, rate: 0.05 } }
    simulated_timezone: America/New_York

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import copy
from typing import Any


def merge_overlay(
    manifest: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    """Apply an overlay to a manifest, returning a new merged document.

    The merged document is structurally a manifest (so the same Layer-2 checks
    apply). Only the §11.1 knobs are honoured; unknown overlay keys are ignored
    here (their acceptance is the catalog's overlay-schema concern). The input
    manifest is not mutated.
    """
    merged = copy.deepcopy(manifest)
    _apply_probabilities(merged, overlay.get("probabilities", {}) or {})
    _apply_dwell(merged, overlay.get("dwell", {}) or {})
    _apply_catalog_sizes(merged, overlay.get("catalog_sizes", {}) or {})
    _apply_intensity(merged, overlay.get("intensity"))
    _apply_cdc_entities(merged, overlay.get("cdc_entities"))
    _apply_chaos(merged, overlay.get("chaos", {}) or {})
    tz = overlay.get("simulated_timezone")
    if isinstance(tz, str):
        merged.setdefault("metadata", {})["simulated_timezone"] = tz
    return merged


def _find_transition(
    merged: dict[str, Any], key: str
) -> dict[str, Any] | None:
    """Resolve a ``machine.state.to`` overlay key to its transition dict."""
    parts = key.split(".")
    if len(parts) < 3:
        return None
    machine_name, state_name, to_state = parts[0], parts[1], ".".join(parts[2:])
    state = (
        merged.get("state_machines", {})
        .get(machine_name, {})
        .get("states", {})
        .get(state_name, {})
    )
    for transition in state.get("transitions", []) or []:
        if isinstance(transition, dict) and transition.get("to") == to_state:
            result: dict[str, Any] = transition
            return result
    return None


def _apply_probabilities(merged: dict[str, Any], probs: dict[str, Any]) -> None:
    for key, value in probs.items():
        transition = _find_transition(merged, key)
        if transition is not None:
            transition["probability"] = value


def _apply_dwell(merged: dict[str, Any], dwell: dict[str, Any]) -> None:
    for key, value in dwell.items():
        transition = _find_transition(merged, key)
        if transition is not None:
            transition["dwell"] = value


def _apply_catalog_sizes(merged: dict[str, Any], sizes: dict[str, Any]) -> None:
    catalogs = merged.setdefault("seeding", {}).setdefault("catalogs", {})
    for entity, size in sizes.items():
        if entity in catalogs:
            catalogs[entity]["default"] = size


def _apply_intensity(merged: dict[str, Any], intensity: Any) -> None:
    if isinstance(intensity, dict):
        merged["intensity"] = intensity


def _apply_cdc_entities(merged: dict[str, Any], enabled: Any) -> None:
    """Restrict instance-enabled CDC to the overlay subset (R-CDC-M1).

    Sets ``enabled_default`` per the overlay subset on entities the manifest
    already declares; entities not listed are disabled. The subset-of-manifest
    check (R-CDC-M1) for undeclared entities is enforced in
    :func:`validate.validate_overlay`, which sees the raw overlay and can attach an
    override-scope MAN-V108 with a precise pointer.
    """
    if not isinstance(enabled, list):
        return
    cdc_entities = merged.get("cdc", {}).get("entities", {})
    enabled_set = set(enabled)
    for ename, cfg in cdc_entities.items():
        cfg["enabled_default"] = ename in enabled_set


def _apply_chaos(merged: dict[str, Any], chaos: dict[str, Any]) -> None:
    if chaos:
        merged.setdefault("chaos_defaults", {}).update(chaos)
