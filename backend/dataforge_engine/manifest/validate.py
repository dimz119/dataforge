"""The public manifest-validation entrypoints (the §8 pipeline, Layers 1+2).

``validate_manifest`` runs the full Phase-3 pipeline on a raw or parsed document:
hardened parse (MAN-S001/2/3) → Layer 1 JSON Schema conformance (MAN-S004) →
Layer 2 semantic checks (MAN-V1xx referential, V2xx machine-structure, V3xx
bounds, V4xx generators, V5xx schema-compat). Layer 3 (dry-run, MAN-D6xx) is
Phase 4 — not run here (§8.4 sequencing).

``validate_overlay`` re-validates a **merged** manifest+overlay document for a
workspace configuration write (§11.1); errors carry ``scope: "override"``.

Sequencing (§8.4): parse failures short-circuit (no structure to validate); a
Layer-1 failure short-circuits Layer 2 (the semantic checks assume a structurally
valid document). Within a layer, all findings are collected.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCollector, ValidationReport, json_pointer
from .model import ManifestView
from .overlay import merge_overlay
from .parse import ManifestParseError, layer1_errors, parse_manifest_text
from .semantic_bounds import check_bounds
from .semantic_compat import PriorSchemaProvider, check_compat
from .semantic_generators import check_generators
from .semantic_machine import check_machines
from .semantic_referential import check_referential

__all__ = [
    "run_layer2",
    "validate_manifest",
    "validate_overlay",
]


def validate_manifest(
    document: str | dict[str, Any],
    *,
    is_workspace_visibility: bool = False,
    registered_hooks: frozenset[str] = frozenset(),
    prior_schemas: PriorSchemaProvider | None = None,
) -> ValidationReport:
    """Validate a manifest through Layers 1+2 and return a :class:`ValidationReport`.

    ``document`` is either the raw YAML/JSON text (parse-hardened here) or an
    already-parsed ``dict``. ``is_workspace_visibility`` gates hooks (MAN-V404);
    ``registered_hooks`` is the platform hook allowlist (MAN-V403);
    ``prior_schemas`` enables the BACKWARD_ADDITIVE check (MAN-V501) for a re-publish.
    """
    if isinstance(document, str):
        try:
            parsed = parse_manifest_text(document)
        except ManifestParseError as exc:
            return ValidationReport(status="failed", errors=(exc.error,))
    else:
        parsed = document

    # Layer 1 — JSON Schema conformance. A schema failure short-circuits Layer 2.
    l1 = layer1_errors(parsed)
    if l1:
        return ValidationReport(status="failed", errors=tuple(l1))

    # Layer 2 — semantic checks.
    collector = ErrorCollector(scope="manifest")
    run_layer2(
        parsed,
        collector,
        is_workspace_visibility=is_workspace_visibility,
        registered_hooks=registered_hooks,
        prior_schemas=prior_schemas,
    )
    return collector.report()


def run_layer2(
    document: dict[str, Any],
    collector: ErrorCollector,
    *,
    is_workspace_visibility: bool = False,
    registered_hooks: frozenset[str] = frozenset(),
    prior_schemas: PriorSchemaProvider | None = None,
) -> None:
    """Run every Layer-2 check against a Layer-1-valid document into ``collector``.

    Shared by ``validate_manifest`` and ``validate_overlay`` (the override path
    runs the same semantic checks against the merged document).
    """
    view = ManifestView(document)
    check_referential(view, collector)
    check_machines(view, collector)
    check_bounds(view, collector)
    check_generators(
        view,
        collector,
        is_workspace_visibility=is_workspace_visibility,
        registered_hooks=registered_hooks,
    )
    check_compat(view, collector, prior_schemas=prior_schemas)


def validate_overlay(
    manifest: dict[str, Any],
    overlay: dict[str, Any],
) -> ValidationReport:
    """Re-validate a workspace overlay as a merged document (§11.1, override scope).

    Runs Layer 2 against ``merge_overlay(manifest, overlay)`` (Layer 1 and Layer 3
    are not re-run for overlays — an overlay cannot change structure). Errors carry
    ``scope: "override"``. Additionally enforces the override-only knob constraints
    the merge cannot express: probability-override allowance/bounds (V208) and the
    ``cdc_entities`` subset rule (R-CDC-M1 / V108).
    """
    merged = merge_overlay(manifest, overlay)
    collector = ErrorCollector(scope="override")
    _check_override_constraints(manifest, overlay, collector)
    run_layer2(merged, collector)
    return collector.report()


def _check_override_constraints(
    manifest: dict[str, Any],
    overlay: dict[str, Any],
    collector: ErrorCollector,
) -> None:
    """Override-only constraints: probability override allowance + cdc subset."""
    _check_probability_overrides(manifest, overlay, collector)
    _check_cdc_subset(manifest, overlay, collector)


def _check_probability_overrides(
    manifest: dict[str, Any],
    overlay: dict[str, Any],
    collector: ErrorCollector,
) -> None:
    machines = manifest.get("state_machines", {})
    for key, value in (overlay.get("probabilities", {}) or {}).items():
        parts = key.split(".")
        if len(parts) < 3:
            continue
        mname, sname, to_state = parts[0], parts[1], ".".join(parts[2:])
        state = machines.get(mname, {}).get("states", {}).get(sname, {})
        transition = next(
            (t for t in state.get("transitions", []) or [] if t.get("to") == to_state),
            None,
        )
        ppath = json_pointer("probabilities", key)
        if transition is None:
            collector.add(
                "MAN-V101", ppath,
                "probability override targets a transition that does not exist",
                actual=key,
            )
            continue
        override = transition.get("override", {}) or {}
        if not override.get("allowed", True):
            collector.add(
                "MAN-V208", ppath,
                "transition does not allow probability override", actual=key,
            )
            continue
        lo = override.get("min", 0.0)
        hi = override.get("max", 1.0)
        if not (lo <= float(value) <= hi):
            collector.add(
                "MAN-V208", ppath,
                "probability override is outside the allowed bounds",
                bound=[lo, hi],  # type: ignore[arg-type]
                actual=value,
            )


def _check_cdc_subset(
    manifest: dict[str, Any],
    overlay: dict[str, Any],
    collector: ErrorCollector,
) -> None:
    enabled = overlay.get("cdc_entities")
    if not isinstance(enabled, list):
        return
    declared = set((manifest.get("cdc", {}) or {}).get("entities", {}).keys())
    for idx, ename in enumerate(enabled):
        if ename not in declared:
            collector.add(
                "MAN-V108", json_pointer("cdc_entities", idx),
                "overlay enables CDC for an entity not declared in cdc.entities",
                actual=ename,
            )
