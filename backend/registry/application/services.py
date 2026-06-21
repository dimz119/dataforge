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


def get_versions_in_range(
    name: str, from_version: int, to_version: int, *, workspace_id: UUID | None
) -> list[SchemaVersion] | None:
    """The subject's versions in ``[from_version, to_version]`` (ascending).

    Used by the #66 diff to aggregate per-step diffs in version-introduction order
    (§7: "multi-step ranges aggregate the computed per-version diffs"). Returns
    ``None`` if the subject is absent, or if either endpoint version is missing —
    the caller maps that to a 404. A contiguous gapless chain is guaranteed by
    INV-REG-2 (versions are monotonic from 1), so the range is always complete.
    """
    subject = _resolve_subject(name, workspace_id)
    if subject is None:
        return None
    rows = list(
        SchemaVersion.objects.filter(
            subject=subject, version__gte=from_version, version__lte=to_version
        ).order_by("version")
    )
    present = {r.version for r in rows}
    if from_version not in present or to_version not in present:
        return None
    return rows


def subjects_emitted_with_latest(
    manifest: dict[str, Any], *, workspace_id: UUID | None
) -> dict[str, int | None]:
    """Every subject ``manifest`` emits → its latest registered version (or ``None``).

    The materialization + validation seam for stream schema pins (schema-registry
    §10.1). ``derive_subjects`` (the Flow-1 derivation) enumerates exactly the
    subjects this manifest emits — business ``{slug}.{event_type}`` and CDC
    ``{slug}.cdc.{entity}`` — which is the authoritative answer to "subjects the
    pinned manifest emits" (PIN-R1/PIN-R3). Each subject resolves to its latest
    registered version *at this moment* (workspace-first then global, including Flow 2
    evolutions, §5.2); a subject the manifest declares but that has no registered
    version yet maps to ``None`` (a manifest that has never been published — the
    caller treats that as "no materialized pin"). Pure read; no write.
    """
    from registry.infra.derive import derive_subjects

    latest_by_subject: dict[str, int | None] = {}
    for derived in derive_subjects(manifest):
        subject = _resolve_subject(derived.subject, workspace_id)
        if subject is None:
            latest_by_subject[derived.subject] = None
            continue
        versions = _versions_for(subject)
        latest_by_subject[derived.subject] = versions[-1].version if versions else None
    return latest_by_subject


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


@dataclass(frozen=True)
class ScenarioContext:
    """The Flow-2 registration context for a subject's scenario (schema-registry §5.2).

    Carries the owning scenario's id (for the ``≤250 subjects`` cap and version
    ownership), its tenancy (``None`` for a global/builtin scenario), and the
    **latest published manifest** document — the binding-resolution context for the
    REG-C007 check (every added ``from`` path must resolve against it).
    """

    scenario_id: UUID
    workspace_id: UUID | None
    latest_manifest: dict[str, Any]
    latest_manifest_version: str


def scenario_context_for_subject(subject_name: str) -> ScenarioContext | None:
    """Resolve the global scenario + latest published manifest for ``subject``.

    Flow 2 registers only platform-owned (global) subjects (§5.2), so the scenario
    is resolved global-only (``workspace_id IS NULL``). The subject's scenario slug
    is the leading dot-free segment (INV-REG-1). Returns ``None`` when the scenario
    or any published manifest is absent — the command maps that to REG-C011-class
    failures via the registration gate (a subject cannot exist without a manifest).
    Reached through a lazy catalog seam (cross-app application↔application is allowed).
    """
    slug = subject_name.split(".", 1)[0]
    try:
        from catalog.domain.models import STATUS_PUBLISHED, ManifestVersion, Scenario
    except ImportError:  # pragma: no cover - integrated build always has catalog
        return None
    scenario: Any = Scenario.objects.filter(slug=slug, workspace_id__isnull=True).first()
    if scenario is None:
        return None
    published = list(
        ManifestVersion.objects.filter(
            scenario=scenario, status=STATUS_PUBLISHED, workspace_id__isnull=True
        )
    )
    if not published:
        return None
    latest = max(published, key=lambda mv: _semver_key(str(mv.version)))
    return ScenarioContext(
        scenario_id=scenario.id,
        workspace_id=None,
        latest_manifest=dict(latest.manifest),
        latest_manifest_version=str(latest.version),
    )


def _semver_key(version: str) -> tuple[int, ...]:
    """Numeric sort key for a ``MAJOR.MINOR.PATCH`` semver (no pre-release in MVP)."""
    parts = version.split(".")
    key: list[int] = []
    for part in parts:
        try:
            key.append(int(part))
        except ValueError:  # pragma: no cover - SEMVER_PATTERN guarantees integers
            key.append(0)
    return tuple(key)
