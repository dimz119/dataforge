"""Catalog-side facade over the framework-free manifest validator.

The validation engine lives in the framework-free package
(:mod:`dataforge_engine.manifest`) so the behaviour engine (Phase 4) reuses the
same generator catalog and Layer-2 checks. This module is the catalog app's thin
seam onto it: it adds the catalog's policy concerns (the hook registry source, the
manifest visibility) and returns the §8.3 :class:`ValidationReport` the catalog
persists on a ManifestVersion (``validation_report`` JSONB, database-schema §4).

Keeping the import here means callers in the catalog app
(``application.services``, the ``sync_builtin_scenarios`` command, the API
viewset) depend on a stable ``catalog.application`` symbol rather than reaching
into the engine package directly.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import (
    PriorSchemaProvider,
    ValidationReport,
    validate_manifest,
    validate_overlay,
)

__all__ = [
    "registered_value_hooks",
    "validate_catalog_manifest",
    "validate_instance_overlay",
]


def registered_value_hooks() -> frozenset[str]:
    """The platform value-hook allowlist (MAN-V403).

    Phase 3 ships no hooks (the reference scenario is hook-free, P-4); the set is
    empty until platform hooks are registered in ``backend/<app>/hooks/`` (§4.6).
    The behaviour engine (Phase 4) freezes the real registry at process start.
    """
    return frozenset()


def validate_catalog_manifest(
    document: str | dict[str, Any],
    *,
    is_workspace_visibility: bool,
    prior_schemas: PriorSchemaProvider | None = None,
) -> ValidationReport:
    """Validate a manifest for catalog ingest/publish (Layers 1+2, §8.4 sequencing).

    ``is_workspace_visibility`` gates hooks (MAN-V404): builtin/global manifests
    pass ``False``; tenant/LLM (``workspace``) manifests pass ``True``.
    ``prior_schemas`` enables the BACKWARD_ADDITIVE re-publish check (MAN-V501);
    pass the registry-backed provider on a minor/patch re-publish, ``None`` on a
    first publication.
    """
    return validate_manifest(
        document,
        is_workspace_visibility=is_workspace_visibility,
        registered_hooks=registered_value_hooks(),
        prior_schemas=prior_schemas,
    )


def validate_instance_overlay(
    manifest: dict[str, Any],
    overlay: dict[str, Any],
) -> ValidationReport:
    """Re-validate a workspace configuration overlay (§11.1, override scope).

    Runs Layer 2 against the merged manifest+overlay document; errors carry
    ``scope: "override"``. Called on every overlay write on a ScenarioInstance.
    """
    return validate_overlay(manifest, overlay)
