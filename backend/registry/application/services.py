"""Read-query services for the Schema Registry context (api-spec §4.12 #62-65).

The registry is read-only over /api/v1 (schema-registry §7): writes happen only
through manifest publication (``registry.application.registration``). This module
owns the read projections the four read endpoints serve. Subject ownership is
hybrid (database-schema §9.5): a subject read returns global (NULL-workspace)
subjects plus the active workspace's own subjects; the RLS Class-H policy
(registry.infra.rls) backstops it at the row level.

The ``manifest_version`` provenance member (§7 #64/#65) joins a schema version's
``derived_from_definition`` to ``scenario_definitions.version`` — the catalog
context owns that table, reached lazily through a thin seam so the registry app
imports no catalog model directly (the cross-app rule permits application↔
application coupling, but a read join is cleaner as a seam).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db.models import Q

from registry.domain.models import SchemaVersion, Subject


@dataclass(frozen=True)
class SubjectSummary:
    """One subject + its version projection (api-spec §4.12 #62)."""

    subject: Subject
    versions: list[SchemaVersion]

    @property
    def version_numbers(self) -> list[int]:
        return [v.version for v in self.versions]

    @property
    def latest_version(self) -> int | None:
        return self.versions[-1].version if self.versions else None

    @property
    def scenario_slug(self) -> str:
        # The subject name is ``{slug}.{event}`` or ``{slug}.cdc.{entity}``; the
        # slug is the leading dot-free segment (INV-REG-1).
        return self.subject.subject.split(".", 1)[0]


def list_subjects(
    *, workspace_id: UUID | None, scenario_slug: str | None = None
) -> list[SubjectSummary]:
    """List subjects visible to the caller (globals + the workspace's own)."""
    visible = Q(workspace_id__isnull=True)
    if workspace_id is not None:
        visible |= Q(workspace_id=workspace_id)
    qs = Subject.objects.filter(visible)
    if scenario_slug is not None:
        qs = qs.filter(Q(subject=scenario_slug) | Q(subject__startswith=f"{scenario_slug}."))
    rows = list(qs.order_by("subject"))
    return [SubjectSummary(subject=s, versions=_versions_for(s)) for s in rows]


def get_subject(name: str, *, workspace_id: UUID | None) -> SubjectSummary | None:
    """Resolve one subject by name (workspace-first then global)."""
    subject = _resolve_subject(name, workspace_id)
    if subject is None:
        return None
    return SubjectSummary(subject=subject, versions=_versions_for(subject))


def get_version(
    name: str, version: int | str, *, workspace_id: UUID | None
) -> SchemaVersion | None:
    """Resolve one schema version of a subject; ``version`` is an int or ``latest``."""
    subject = _resolve_subject(name, workspace_id)
    if subject is None:
        return None
    qs = SchemaVersion.objects.filter(subject=subject)
    if version == "latest":
        return qs.order_by("-version").first()
    try:
        number = int(version)
    except (ValueError, TypeError):
        return None
    return qs.filter(version=number).first()


def manifest_version_for(schema_version: SchemaVersion) -> str | None:
    """The Flow-1 provenance: the semver of the manifest version that derived this.

    ``None`` for Flow-2 (explicit-evolution) versions (Phase 10) — those have no
    ``derived_from_definition``. Reached through a lazy catalog seam (see module
    docstring).
    """
    definition_id = schema_version.derived_from_definition
    if definition_id is None:
        return None
    return _manifest_semver(definition_id)


def _resolve_subject(name: str, workspace_id: UUID | None) -> Subject | None:
    if workspace_id is not None:
        ws_row = Subject.objects.filter(subject=name, workspace_id=workspace_id).first()
        if ws_row is not None:
            return ws_row
    return Subject.objects.filter(subject=name, workspace_id__isnull=True).first()


def _versions_for(subject: Subject) -> list[SchemaVersion]:
    return list(SchemaVersion.objects.filter(subject=subject).order_by("version"))


def _manifest_semver(definition_id: UUID) -> str | None:
    try:
        from catalog.domain.models import ManifestVersion
    except ImportError:  # pragma: no cover - integrated build always has catalog
        return None
    row: Any = ManifestVersion.objects.filter(id=definition_id).only("version").first()
    return str(row.version) if row is not None else None
