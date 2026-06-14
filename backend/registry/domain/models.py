"""Domain models for the Schema Registry context.

Two hybrid-owned tables (database-schema §4.4-4.5, §9.5): ``schema_subjects``
(aggregate root **Subject**) and ``schema_versions`` (entity **SchemaVersion**).
Both carry a *nullable* ``workspace_id`` — ``NULL`` ⇔ a platform-global builtin
subject readable by every workspace (INV-REG-4); non-null ⇔ a tenant-owned
subject derived from a ``workspace``-visibility scenario. Because the tenant
column is nullable, these models cannot subclass ``WorkspaceScopedModel`` (whose
fail-closed scoped manager assumes a non-null tenant column) — they are listed in
``tenancy_exempt`` (hybrid §9.5) and the app owns its own RLS (Class H,
``registry.infra.rls``). Every concrete model sets ``Meta.db_table`` explicitly
to the name fixed by database-schema.md (rule BE-APP-1, C-2).

Immutability (INV-REG-2): there is no application update/delete path for either
model once written; the publish transaction (``registry.application.publish``) is
the only writer. The §4.5 ``BEFORE UPDATE OR DELETE`` row trigger and the
``dataforge_app`` grant matrix backstop this at the DB; the app exposes no
mutating viewset, serializer, or admin action.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models
from django.utils import timezone

from registry.domain.ids import uuid4

# The §2.1 subject structural regex (database-schema §4.4 ``schema_subjects`` CHECK):
# {scenario_slug}.{event_type} for business events, {scenario_slug}.cdc.{entity}
# for CDC row images (INV-REG-1).
SUBJECT_PATTERN = r"^[a-z][a-z0-9_]*(\.cdc)?\.[a-z][a-z0-9_]*$"

# MVP single compatibility mode (domain model §2.4 CompatibilityMode); the column
# exists so a future mode lands as data + a widened CHECK, not a redesign.
COMPAT_BACKWARD_ADDITIVE = "BACKWARD_ADDITIVE"
_COMPAT_CHOICES: list[tuple[str, str]] = [
    (COMPAT_BACKWARD_ADDITIVE, "Backward additive"),
]


class Subject(models.Model):
    """One registry subject (schema-registry §3.2, INV-REG-1).

    Subject ownership mirrors the scenario that derived it (§2.3): ``workspace_id``
    is ``NULL`` for global/builtin subjects (unique on ``subject`` alone) and set
    for tenant subjects (unique on ``(workspace_id, subject)``).
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    subject = models.TextField()  # CHECK against SUBJECT_PATTERN (constraint below)
    scenario_id = models.UUIDField()  # FK → scenarios.id (cross-app; no ORM relation)
    workspace_id = models.UUIDField(null=True, blank=True)  # NULL ⇒ global/builtin (C-8)
    compatibility_mode = models.TextField(
        choices=_COMPAT_CHOICES, default=COMPAT_BACKWARD_ADDITIVE
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "schema_subjects"  # C-2: fixed by database-schema §4.4
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # database-schema §4.4 ``schema_subjects_compat_ck``.
            models.CheckConstraint(
                condition=models.Q(compatibility_mode=COMPAT_BACKWARD_ADDITIVE),
                name="schema_subjects_compat_ck",
            ),
            # database-schema §4.4 partial uniques: global vs workspace namespaces.
            models.UniqueConstraint(
                fields=["subject"],
                condition=models.Q(workspace_id__isnull=True),
                name="schema_subjects_global_uq",
            ),
            models.UniqueConstraint(
                fields=["workspace_id", "subject"],
                condition=models.Q(workspace_id__isnull=False),
                name="schema_subjects_ws_uq",
            ),
        ]

    def __str__(self) -> str:
        return self.subject


class SchemaVersion(models.Model):
    """One immutable schema version of a subject (schema-registry §3.2, INV-REG-2).

    ``version`` is server-assigned, gapless, monotonic from 1 per subject;
    ``fingerprint`` is the SHA-256 of the comparison-form canonical JSON (§6.1) and
    is unique per subject so fingerprint equality ⇔ no schema change (R-DER-4).
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    subject = models.ForeignKey(
        Subject,
        on_delete=models.RESTRICT,
        db_column="subject_id",
        related_name="versions",
    )
    workspace_id = models.UUIDField(null=True, blank=True)  # denormalized (C-8)
    version = models.IntegerField()  # monotonic per subject, >= 1 (INV-REG-2)
    json_schema = models.JSONField()  # closed JSON Schema document (R-DER-3)
    fingerprint = models.TextField()  # SHA-256 hex of comparison-form canonical JSON
    compat_checked_against = models.IntegerField(null=True, blank=True)  # NULL for v1
    derived_from_definition = models.UUIDField(null=True, blank=True)  # provenance (R-DER-4)
    registered_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "schema_versions"  # C-2: fixed by database-schema §4.5
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="schema_versions_version_ck",
            ),
            models.UniqueConstraint(
                fields=["subject", "version"],
                name="schema_versions_subject_uq",
            ),
            models.UniqueConstraint(
                fields=["subject", "fingerprint"],
                name="schema_versions_fp_uq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject_id}:{self.version}"
