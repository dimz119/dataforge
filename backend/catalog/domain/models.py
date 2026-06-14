"""Domain models for the Scenario Catalog context.

Three tables (database-schema §4.1-4.3):

* ``scenarios`` (aggregate root **Scenario**) — hybrid (§9.5 Class H):
  ``visibility = global`` ⇔ ``workspace_id IS NULL`` (platform-curated builtins,
  readable by everyone); ``visibility = workspace`` ⇔ ``workspace_id`` set (the
  AI-manifest seam, tenant-owned, INV-CAT-6).
* ``scenario_definitions`` (entity **ManifestVersion**) — hybrid (Class H), one
  row per draft/published/deprecated manifest version with the canonical-JSON
  document, its sha256, the persisted §8.3 ValidationReport, and the
  draft→published→deprecated lifecycle (INV-CAT-1/2/5).
* ``workspace_scenario_configs`` (aggregate **ScenarioInstance**) — standard
  Class T tenant table (non-null ``workspace_id``): the workspace overlay +
  ``config_revision`` + the pinned manifest version (PIN-1..5, §11).

The two hybrid tables carry a nullable ``workspace_id`` so they cannot use the
fail-closed ``WorkspaceScoped`` manager — they are listed in ``tenancy_exempt``
(hybrid §9.5) and the app owns Class H RLS (``catalog.infra.rls``). The instance
table is a normal tenant model and subclasses ``WorkspaceScopedModel`` so the
``check_tenancy`` guard's tenant assertions apply to it.

Every concrete model sets ``Meta.db_table`` explicitly (rule BE-APP-1, C-2).
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models
from django.utils import timezone

from catalog.domain.ids import uuid4
from tenancy.domain.scoping import WorkspaceScopedModel

# --- Scenario visibility (database-schema §4.1 ``scenarios_visibility_ck``) ----
VISIBILITY_GLOBAL = "global"
VISIBILITY_WORKSPACE = "workspace"
VISIBILITIES: tuple[str, str] = (VISIBILITY_GLOBAL, VISIBILITY_WORKSPACE)
_VISIBILITY_CHOICES: list[tuple[str, str]] = [
    (VISIBILITY_GLOBAL, "Global"),
    (VISIBILITY_WORKSPACE, "Workspace"),
]

# --- ManifestVersion lifecycle (database-schema §4.2 ``scenario_defs_status_ck``)
STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"
STATUS_DEPRECATED = "deprecated"
STATUSES: tuple[str, str, str] = (STATUS_DRAFT, STATUS_PUBLISHED, STATUS_DEPRECATED)
_STATUS_CHOICES: list[tuple[str, str]] = [
    (STATUS_DRAFT, "Draft"),
    (STATUS_PUBLISHED, "Published"),
    (STATUS_DEPRECATED, "Deprecated"),
]

# Slug grammar (database-schema §4.1) and semver (§4.2).
SLUG_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"


class Scenario(models.Model):
    """A scenario root (database-schema §4.1, domain model **Scenario**).

    Global (NULL-workspace) and workspace namespaces are disjoint at resolution;
    the application forbids a workspace slug that collides with a global slug
    (§4.1 slug-resolution rule, enforced in ``catalog.application``).
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    slug = models.TextField()  # CHECK against SLUG_PATTERN (constraint below)
    title = models.TextField()
    description = models.TextField(default="")
    visibility = models.TextField(choices=_VISIBILITY_CHOICES)
    workspace_id = models.UUIDField(null=True, blank=True)  # NULL ⇔ global (C-8)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "scenarios"  # C-2: fixed by database-schema §4.1
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(visibility__in=VISIBILITIES),
                name="scenarios_visibility_ck",
            ),
            # database-schema §4.1 ``scenarios_visibility_ws_ck``:
            # (visibility = 'global') = (workspace_id IS NULL).
            models.CheckConstraint(
                condition=(
                    models.Q(visibility=VISIBILITY_GLOBAL, workspace_id__isnull=True)
                    | models.Q(visibility=VISIBILITY_WORKSPACE, workspace_id__isnull=False)
                ),
                name="scenarios_visibility_ws_ck",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(workspace_id__isnull=True),
                name="scenarios_global_slug_uq",
            ),
            models.UniqueConstraint(
                fields=["workspace_id", "slug"],
                condition=models.Q(workspace_id__isnull=False),
                name="scenarios_ws_slug_uq",
            ),
        ]

    def __str__(self) -> str:
        return self.slug


class ManifestVersion(models.Model):
    """One manifest version (database-schema §4.2 ``scenario_definitions``).

    The table name is ``scenario_definitions`` (the persistence authority); the
    domain/API term is *manifest version*. ``manifest`` holds the canonical JSON
    that conforms to the Manifest v0 JSON Schema; ``manifest_sha256`` is the hash
    of that canonical JSON (the builtin loader's drift detector, §10.2).
    ``validation_report`` persists the §8.3 ValidationReport (INV-CAT-2).
    Published versions are immutable forever (INV-CAT-1) — there is no application
    update path for ``manifest``/``manifest_sha256``/``version``/``builtin`` once
    ``status != 'draft'``; the §4.2 BEFORE-UPDATE trigger backstops it at the DB.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    scenario = models.ForeignKey(
        Scenario,
        on_delete=models.RESTRICT,
        db_column="scenario_id",
        related_name="definitions",
    )
    workspace_id = models.UUIDField(null=True, blank=True)  # denormalized (C-8)
    version = models.TextField()  # semver, CHECK against SEMVER_PATTERN
    manifest = models.JSONField()  # canonical JSON, conforms to Manifest v0 schema
    manifest_sha256 = models.TextField()
    builtin = models.BooleanField(default=False)
    status = models.TextField(choices=_STATUS_CHOICES, default=STATUS_DRAFT)
    validation_report = models.JSONField(default=dict)  # §8.3 report (INV-CAT-2)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "scenario_definitions"  # C-2: fixed by database-schema §4.2
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(status__in=STATUSES),
                name="scenario_defs_status_ck",
            ),
            # §4.2 ``scenario_defs_published_ck``: a non-draft row carries published_at.
            models.CheckConstraint(
                condition=(
                    models.Q(status=STATUS_DRAFT) | models.Q(published_at__isnull=False)
                ),
                name="scenario_defs_published_ck",
            ),
            models.UniqueConstraint(
                fields=["scenario", "version"],
                name="scenario_defs_version_uq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["scenario", "status"], name="scenario_defs_scenario_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.scenario_id}:{self.version}"


class ScenarioInstance(WorkspaceScopedModel):
    """A workspace scenario configuration (database-schema §4.3, **ScenarioInstance**).

    The table name is ``workspace_scenario_configs``; the API/domain term is
    *scenario instance*. A standard Class T tenant model (non-null ``workspace_id``)
    so it uses the scoped manager + tenancy RLS. ``overrides`` is the workspace
    overlay re-validated on every write (INV-CAT-3); ``config_version`` increments
    per overlay/re-pin edit (the ``config_revision`` of PIN-2/§11.2).
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    # Non-null tenant column (C-8). FK declared here per the WorkspaceScopedModel
    # contract (the abstract base does not declare the column itself).
    workspace = models.ForeignKey(
        "tenancy.Workspace",
        on_delete=models.RESTRICT,
        db_column="workspace_id",
        related_name="scenario_instances",
    )
    scenario = models.ForeignKey(
        Scenario,
        on_delete=models.RESTRICT,
        db_column="scenario_id",
        related_name="instances",
    )
    scenario_definition = models.ForeignKey(
        ManifestVersion,
        on_delete=models.RESTRICT,
        db_column="scenario_definition_id",
        related_name="instances",
    )  # the pinned manifest version
    name = models.TextField()
    overrides = models.JSONField(default=dict)
    config_version = models.IntegerField(default=1)  # config_revision (PIN-2)
    default_seed = models.BigIntegerField(null=True, blank=True)
    created_by = models.UUIDField()  # FK → users.id (cross-app; no ORM relation)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(WorkspaceScopedModel.Meta):
        db_table = "workspace_scenario_configs"  # C-2: fixed by database-schema §4.3
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    models.Q(default_seed__gte=0) | models.Q(default_seed__isnull=True)
                ),
                name="ws_scenario_configs_seed_ck",
            ),
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="ws_scenario_configs_name_uq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace"], name="ws_scenario_configs_ws_ix"),
        ]

    def __str__(self) -> str:
        return self.name
