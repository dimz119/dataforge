"""Read-query + instance use-case services for the Scenario Catalog context.

The write path (draft ingest, publish) lives in ``ingest``/``publish``; this
module owns the catalog **read** projections the API serves (api-spec §4.6 #26-29
scenarios, §4.7 #33-38 scenario instances) plus the ScenarioInstance create/edit
use cases (PIN-1..5, §11). All queries respect the hybrid ownership model
(database-schema §9.5): a scenario read returns global (NULL-workspace) rows plus
the active workspace's own rows; an instance read is plain Class-T scoped.

Services own the transaction boundary and orchestrate domain models plus infra
adapters (backend-architecture §3.1, application layer).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from catalog.application import audit
from catalog.application.ingest import resolve_scenario
from catalog.application.validation import validate_instance_overlay
from catalog.domain.models import (
    STATUS_DEPRECATED,
    STATUS_PUBLISHED,
    VISIBILITY_GLOBAL,
    ManifestVersion,
    Scenario,
    ScenarioInstance,
)


class InstancePinDeprecated(Exception):
    """Pinning a ``deprecated`` manifest version is forbidden (INV-CAT-5, 409)."""


class InstanceOverlayRejected(Exception):
    """An instance overlay failed Layer-2 re-validation; carries the report (422)."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("overlay validation failed")


class InstanceHasStreams(Exception):
    """A non-deleted stream still references this instance; delete blocked (409)."""


class InstanceNameConflict(Exception):
    """The (workspace, name) pair is taken (409)."""


@dataclass(frozen=True)
class ScenarioSummary:
    """One scenario row + its published-version projection (api-spec §4.6)."""

    scenario: Scenario
    versions: list[ManifestVersion]

    @property
    def published_versions(self) -> list[str]:
        return [v.version for v in self.versions if v.status == STATUS_PUBLISHED]

    @property
    def latest_version(self) -> str | None:
        published = self.published_versions
        return published[-1] if published else None


# --- scenario reads ---------------------------------------------------------
def list_scenarios(
    *, workspace_id: UUID | None, visibility: str | None = None
) -> list[ScenarioSummary]:
    """List scenarios visible to the caller (globals + the workspace's own).

    Global (NULL-workspace) rows are visible to everyone; a workspace's own rows
    are included when ``workspace_id`` is set (the caller's armed workspace). The
    optional ``visibility`` filter narrows to ``global`` or ``workspace``.
    """
    visible = Q(workspace_id__isnull=True)
    if workspace_id is not None:
        visible |= Q(workspace_id=workspace_id)
    qs = Scenario.objects.filter(visible)
    if visibility is not None:
        qs = qs.filter(visibility=visibility)
    rows = list(qs.order_by("slug"))
    return [ScenarioSummary(scenario=s, versions=_versions_for(s)) for s in rows]


def get_scenario(slug: str, *, workspace_id: UUID | None) -> ScenarioSummary | None:
    """Resolve one scenario (workspace-first then global) + its versions."""
    scenario = resolve_scenario(slug, workspace_id)
    if scenario is None:
        return None
    return ScenarioSummary(scenario=scenario, versions=_versions_for(scenario))


def get_manifest_version(
    slug: str, version: str, *, workspace_id: UUID | None
) -> ManifestVersion | None:
    """Resolve one manifest version of a scenario by semver string."""
    scenario = resolve_scenario(slug, workspace_id)
    if scenario is None:
        return None
    return ManifestVersion.objects.filter(scenario=scenario, version=version).first()


def _versions_for(scenario: Scenario) -> list[ManifestVersion]:
    return list(
        ManifestVersion.objects.filter(scenario=scenario).order_by("version")
    )


# --- scenario instances (§4.7) ----------------------------------------------
def list_instances(*, workspace: Any) -> list[ScenarioInstance]:
    """The workspace's scenario instances (Class-T scoped manager)."""
    return list(ScenarioInstance.objects.filter(workspace=workspace).order_by("-created_at"))


def get_instance(instance_id: UUID, *, workspace: Any) -> ScenarioInstance | None:
    """One instance by id within the active workspace (foreign id → None → 404)."""
    return ScenarioInstance.objects.filter(id=instance_id, workspace=workspace).first()


def create_instance(
    *,
    workspace: Any,
    name: str,
    scenario_slug: str,
    manifest_version: str,
    configuration: dict[str, Any] | None,
    actor: Any,
    default_seed: int | None = None,
) -> ScenarioInstance:
    """Pin a published manifest version + overlay into a workspace instance (§11).

    The pinned version must be ``published`` and not ``deprecated`` (INV-CAT-5).
    The overlay is re-validated as a merged document (scope ``override``) before
    any row is written (INV-CAT-3). ``config_revision`` starts at 1 (PIN-2).
    """
    overlay = configuration or {}
    definition = _resolve_pinnable(scenario_slug, manifest_version, workspace_id=workspace.id)
    _revalidate_overlay(definition.manifest, overlay)
    if ScenarioInstance.objects.filter(workspace=workspace, name=name).exists():
        raise InstanceNameConflict(f"an instance named '{name}' already exists")
    with transaction.atomic():
        instance: ScenarioInstance = ScenarioInstance.objects.create(
            workspace=workspace,
            scenario=definition.scenario,
            scenario_definition=definition,
            name=name,
            overrides=overlay,
            config_version=1,
            default_seed=default_seed,
            created_by=getattr(actor, "id", None),
        )
        audit.emit(
            "catalog.scenario_instance.created",
            actor=actor,
            workspace_id=workspace.id,
            target={"type": "scenario_instance", "id": str(instance.id), "label": name},
            metadata={"scenario_slug": scenario_slug, "manifest_version": manifest_version},
        )
    return instance


def replace_configuration(
    *, instance: ScenarioInstance, configuration: dict[str, Any], actor: Any
) -> ScenarioInstance:
    """Full overlay replacement + Layer-2 re-validation; bumps config_revision (PIN-2).

    Failure → :class:`InstanceOverlayRejected` and **no** revision is written.
    """
    _revalidate_overlay(instance.scenario_definition.manifest, configuration)
    with transaction.atomic():
        instance.overrides = configuration
        instance.config_version = instance.config_version + 1
        instance.save(update_fields=["overrides", "config_version", "updated_at"])
        audit.emit(
            "catalog.scenario_instance.reconfigured",
            actor=actor,
            workspace_id=instance.workspace_id,
            target={"type": "scenario_instance", "id": str(instance.id), "label": instance.name},
            metadata={"config_revision": instance.config_version},
        )
    return instance


def delete_instance(*, instance: ScenarioInstance, actor: Any) -> None:
    """Delete an instance, blocked while any non-deleted stream references it (409).

    Stream references are checked through the streams app's reader seam when it
    exists (Phase 5); in the Phase-3 isolated build the seam is absent and the
    delete proceeds (no streams can exist yet).
    """
    if _instance_has_live_streams(instance.id):
        raise InstanceHasStreams("delete the streams referencing this instance first")
    with transaction.atomic():
        audit.emit(
            "catalog.scenario_instance.deleted",
            actor=actor,
            workspace_id=instance.workspace_id,
            target={"type": "scenario_instance", "id": str(instance.id), "label": instance.name},
            metadata={},
        )
        instance.delete()


def _resolve_pinnable(
    scenario_slug: str, manifest_version: str, *, workspace_id: UUID
) -> ManifestVersion:
    definition = get_manifest_version(scenario_slug, manifest_version, workspace_id=workspace_id)
    if definition is None:
        from config.problems import NotFoundError

        raise NotFoundError()
    if definition.status == STATUS_DEPRECATED:
        raise InstancePinDeprecated(
            f"{scenario_slug}:{manifest_version} is deprecated and cannot be pinned (INV-CAT-5)."
        )
    if definition.status != STATUS_PUBLISHED:
        raise InstancePinDeprecated(
            f"{scenario_slug}:{manifest_version} is not published; only published versions pin."
        )
    return definition


def _revalidate_overlay(manifest: dict[str, Any], overlay: dict[str, Any]) -> None:
    report = validate_instance_overlay(manifest, overlay)
    if not report.passed:
        raise InstanceOverlayRejected(report.to_dict())


def _instance_has_live_streams(instance_id: UUID) -> bool:
    """True iff a non-deleted stream references this instance (streams seam, Phase 5)."""
    try:
        from streams.application.reader import (  # type: ignore[import-not-found]
            instance_has_live_streams,
        )
    except ImportError:
        return False  # Phase-3 build: the streams reader seam lands in Phase 5
    return bool(instance_has_live_streams(instance_id))


def visibility_for(scenario: Scenario) -> str:
    """The scenario's visibility (``global`` ⇔ NULL workspace)."""
    return VISIBILITY_GLOBAL if scenario.workspace_id is None else scenario.visibility
