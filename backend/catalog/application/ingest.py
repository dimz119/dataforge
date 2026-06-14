"""Manifest ingestion + scenario resolution (plugin-arch §10.1, §12; api §4.6).

This module owns the catalog write path the API and the builtin-sync command share:

* ``resolve_scenario`` — slug → Scenario, **workspace-first then global** (§4.1
  slug-resolution rule); global and workspace namespaces are disjoint.
* ``create_draft`` — parse-hardened ingest + Layers 1+2 (synchronous, §8.4
  sequencing) → a ``draft`` ManifestVersion carrying the canonical JSON, its
  sha256, and the persisted §8.3 ValidationReport. Validation failure raises
  :class:`ManifestRejected` (the API maps it to 422 manifest-validation-failed and
  creates no draft). Hook generators in a ``workspace`` manifest fail validation
  (MAN-V404) inside the report. A colliding ``(slug, version)`` raises
  :class:`VersionConflict` (409). A workspace slug colliding with a global slug
  raises :class:`SlugCollision` (the §4.1 app-validated rule).

``is_workspace_visibility`` gates hooks (False for builtins/global, True for tenant
manifests) and selects ownership: global manifests own a NULL ``workspace_id``;
workspace manifests carry the tenant id (INV-CAT-6, the AI-manifest seam).

Service layer; the validator/canonicalization is the engine package
(``dataforge_engine.manifest``), reached through ``catalog.application.validation``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from catalog.application.validation import validate_catalog_manifest
from catalog.domain.models import (
    STATUS_DRAFT,
    VISIBILITY_GLOBAL,
    VISIBILITY_WORKSPACE,
    ManifestVersion,
    Scenario,
)
from dataforge_engine.manifest import ManifestParseError, parse_manifest_text

# ``ManifestParseError`` is re-exported so the API viewset catches a single
# ``catalog.application.ingest`` symbol for parse-hardening failures (MAN-S001/2/3)
# rather than reaching into the engine package from the api layer.
__all__ = [
    "MAX_DRAFT_VERSIONS_PER_WORKSPACE",
    "CanonicalManifest",
    "DraftQuotaExceeded",
    "ManifestParseError",
    "ManifestRejected",
    "SlugCollision",
    "VersionConflict",
    "canonicalize",
    "create_draft",
    "enforce_draft_quota",
    "resolve_scenario",
]


class ManifestRejected(Exception):
    """L1/L2 (or parse-hardening) rejected the manifest; carries the report dict."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("manifest validation failed")


class DraftQuotaExceeded(Exception):
    """The workspace already holds the AI-4 maximum of ``draft`` versions (≤ 20)."""


# AI-4 anti-DoS quotas (plugin-arch §12, AI-4). The ≤30-validation-runs/hour rate
# bucket is enforced at the API edge with the rate-limit stack (the ``validator``
# bucket, api-spec §2.8); this draft cap is a DB-count guard enforced here.
MAX_DRAFT_VERSIONS_PER_WORKSPACE = 20


def enforce_draft_quota(workspace_id: UUID | None) -> None:
    """Raise :class:`DraftQuotaExceeded` if the workspace is at the AI-4 draft cap.

    Only applies to workspace-visibility ingest (``workspace_id`` set); builtins
    (``None``) are platform-curated and exempt.
    """
    if workspace_id is None:
        return
    drafts = ManifestVersion.objects.filter(
        workspace_id=workspace_id, status=STATUS_DRAFT
    ).count()
    if drafts >= MAX_DRAFT_VERSIONS_PER_WORKSPACE:
        raise DraftQuotaExceeded(
            f"workspace already holds {drafts} draft manifest versions "
            f"(AI-4 limit {MAX_DRAFT_VERSIONS_PER_WORKSPACE}); publish or delete some."
        )


class VersionConflict(Exception):
    """A draft/published ``(scenario, version)`` already exists (409 conflict)."""


class SlugCollision(Exception):
    """A workspace scenario slug collides with a global slug (§4.1 rule)."""


@dataclass(frozen=True)
class CanonicalManifest:
    """A parsed, canonicalized manifest + its sha256 (database-schema §4.2)."""

    document: dict[str, Any]
    sha256: str
    slug: str
    version: str


def canonicalize(document_text_or_obj: str | dict[str, Any]) -> CanonicalManifest:
    """Parse-harden (if text) and produce the canonical JSON + sha256.

    The canonical JSON is the byte-stable RFC-8785-style form (sorted keys, compact
    separators) the catalog stores and hashes — the builtin loader's drift detector
    (§10.2) compares this hash against the repo YAML.
    """
    if isinstance(document_text_or_obj, str):
        document = parse_manifest_text(document_text_or_obj)
    else:
        document = document_text_or_obj
    canonical_bytes = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    metadata = document.get("metadata", {}) if isinstance(document, dict) else {}
    return CanonicalManifest(
        document=document,
        sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        slug=str(metadata.get("slug", "")),
        version=str(metadata.get("version", "")),
    )


def resolve_scenario(slug: str, workspace_id: UUID | None) -> Scenario | None:
    """Resolve ``slug`` workspace-first then global (§4.1). ``None`` if absent.

    Uses ``all_objects``-equivalent plain manager (the Scenario model is hybrid, not
    Class T); RLS Class H + the explicit ``workspace_id`` filter carry isolation.
    """
    if workspace_id is not None:
        ws_row = Scenario.objects.filter(slug=slug, workspace_id=workspace_id).first()
        if ws_row is not None:
            return ws_row
    return Scenario.objects.filter(slug=slug, workspace_id__isnull=True).first()


def create_draft(
    document_text_or_obj: str | dict[str, Any],
    *,
    workspace_id: UUID | None,
    is_workspace_visibility: bool,
    title: str | None = None,
    description: str = "",
    builtin: bool = False,
) -> ManifestVersion:
    """Validate (L1+L2) and persist a ``draft`` manifest version (§8.4, §4.6).

    Raises :class:`ManifestParseError` on parse-hardening failure (MAN-S001/2/3),
    :class:`ManifestRejected` on L1/L2 failure, :class:`VersionConflict` on a
    duplicate ``(slug, version)``, or :class:`SlugCollision` on a workspace slug
    that shadows a global slug.
    """
    canonical = canonicalize(document_text_or_obj)
    report = validate_catalog_manifest(
        canonical.document, is_workspace_visibility=is_workspace_visibility
    )
    if not report.passed:
        raise ManifestRejected(report.to_dict())

    scenario = _ensure_scenario(
        slug=canonical.slug,
        workspace_id=workspace_id,
        is_workspace_visibility=is_workspace_visibility,
        title=title or canonical.document.get("metadata", {}).get("title", canonical.slug),
        description=description,
    )
    if ManifestVersion.objects.filter(scenario=scenario, version=canonical.version).exists():
        raise VersionConflict(f"{canonical.slug}:{canonical.version} already exists.")

    return ManifestVersion.objects.create(
        scenario=scenario,
        workspace_id=workspace_id,
        version=canonical.version,
        manifest=canonical.document,
        manifest_sha256=canonical.sha256,
        builtin=builtin,
        status=STATUS_DRAFT,
        validation_report=report.to_dict(),
    )


def _ensure_scenario(
    *,
    slug: str,
    workspace_id: UUID | None,
    is_workspace_visibility: bool,
    title: str,
    description: str,
) -> Scenario:
    """Find-or-create the scenario root for ``slug`` in the right namespace."""
    visibility = VISIBILITY_WORKSPACE if is_workspace_visibility else VISIBILITY_GLOBAL
    if is_workspace_visibility:
        # §4.1: a workspace slug may not collide with a global slug.
        if Scenario.objects.filter(slug=slug, workspace_id__isnull=True).exists():
            raise SlugCollision(f"slug '{slug}' is reserved by a global scenario.")
        existing = Scenario.objects.filter(slug=slug, workspace_id=workspace_id).first()
    else:
        existing = Scenario.objects.filter(slug=slug, workspace_id__isnull=True).first()
    if existing is not None:
        return existing
    return Scenario.objects.create(
        slug=slug,
        title=title,
        description=description,
        visibility=visibility,
        workspace_id=workspace_id if is_workspace_visibility else None,
    )
