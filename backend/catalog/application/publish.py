"""The manifest-publish transaction (plugin-arch §10.3; schema-registry §5.1, R-DER).

``publish_manifest_version`` is the single publish path for both builtin sync and
the catalog API. It runs **one transaction** (INV-CAT-2 + R-DER): either the
manifest version becomes ``published`` *and* every derived schema commits, or
neither. Steps:

1. Require the persisted ValidationReport passed (INV-CAT-2). A draft that has not
   passed L1+L2 cannot publish → :class:`PublishNotReady`.
2. Flip the row to ``published`` with ``published_at`` stamped.
3. Derive a v1 closed JSON Schema for every event-type subject ``{slug}.{event}``
   and CDC subject ``{slug}.cdc.{entity}`` (registry.infra.derive, R-DER-1..3) and
   register version 1 for each in the registry — all in this transaction
   (registry.application.register_derived_schemas). Re-derivation is byte-identical.
4. A non-additive derived schema (only reachable on a *re-publish* minor version)
   fails registration; the registry raises ``SchemaCompatibilityError`` →
   re-raised as :class:`ManifestSchemaCompatError` carrying MAN-V501 errors so the
   API surfaces a 422 manifest-validation-failed at the manifest (§6.3).
5. Emit ``catalog.scenario.published`` plus ``registry.schema_version.registered``
   per newly registered version (INV-AUD-2, transactional).

Builtin (global) scenarios publish with ``workspace_id = None`` (their subjects own
a NULL workspace); tenant scenarios publish under their workspace. The caller arms
the right DB context (the maintenance role for global rows; the request workspace
for tenant rows) — this service is context-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from catalog.application import audit
from catalog.domain.models import STATUS_PUBLISHED, ManifestVersion
from registry.application.registration import (
    RegisteredVersion,
    SchemaCompatibilityError,
    register_derived_schemas,
)


class PublishNotReady(Exception):
    """The draft's ValidationReport has not passed; publication is blocked (INV-CAT-2)."""


class AlreadyPublished(Exception):
    """The version is already published (idempotency guard for the sync command)."""


@dataclass(frozen=True)
class ManifestSchemaCompatError(Exception):
    """A derived schema is not BACKWARD_ADDITIVE-compatible — fails publication.

    Carries one ``MAN-V501`` error per REG-C violation in the §6.3 wire shape
    (``{code, scope, path, message}``), ``scope: "schema"``. The API maps this to
    a 422 ``manifest-validation-failed`` (the failure surfaces at the manifest).
    """

    subject: str
    errors: list[dict[str, str]]

    def __str__(self) -> str:
        return f"derived schema for {self.subject} is not BACKWARD_ADDITIVE-compatible"


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a publish: the row + the registered/no-op'd subject versions."""

    manifest_version: ManifestVersion
    registered: list[RegisteredVersion]


def publish_manifest_version(
    definition: ManifestVersion,
    *,
    actor: Any,
    workspace_id: UUID | None,
) -> PublishResult:
    """Publish ``definition`` and derive+register its schemas in one transaction."""
    report = definition.validation_report or {}
    if report.get("status") != "passed":
        raise PublishNotReady(
            "The manifest version has not passed validation (INV-CAT-2)."
        )

    with transaction.atomic():
        # Re-read under the row lock to make the publish idempotent + race-free.
        locked = (
            ManifestVersion.objects.select_for_update().filter(pk=definition.pk).first()
        )
        if locked is None:  # pragma: no cover - defensive
            raise PublishNotReady("Manifest version no longer exists.")
        if locked.status == STATUS_PUBLISHED:
            raise AlreadyPublished(f"{locked.scenario_id}:{locked.version} already published.")

        locked.status = STATUS_PUBLISHED
        locked.published_at = timezone.now()
        locked.save(update_fields=["status", "published_at"])

        try:
            registered = register_derived_schemas(
                locked.manifest,
                scenario_id=locked.scenario_id,
                workspace_id=workspace_id,
                definition_id=locked.id,
                on_registered=lambda *, subject, version, workspace_id: _audit_registered(
                    actor=actor, subject=subject, version=version, workspace_id=workspace_id
                ),
            )
        except SchemaCompatibilityError as exc:
            raise ManifestSchemaCompatError(
                subject=exc.subject,
                errors=[_to_man_v501(err.to_dict(), exc.subject) for err in exc.errors],
            ) from exc

        audit.emit(
            "catalog.scenario.published",
            actor=actor,
            workspace_id=workspace_id,
            target={
                "type": "manifest_version",
                "id": str(locked.id),
                "label": f"{locked.scenario_id}:{locked.version}",
            },
            metadata={
                "version": locked.version,
                "subjects_registered": [r.subject for r in registered if r.created],
            },
        )
        return PublishResult(manifest_version=locked, registered=registered)


def _audit_registered(
    *, actor: Any, subject: str, version: int, workspace_id: UUID | None
) -> None:
    """Emit the per-version ``registry.schema_version.registered`` audit (§5)."""
    audit.emit(
        "registry.schema_version.registered",
        actor=actor,
        workspace_id=workspace_id,
        target={"type": "schema_version", "id": f"{subject}:{version}", "label": subject},
        metadata={"subject": subject, "version": version},
    )


def _to_man_v501(reg_error: dict[str, str], subject: str) -> dict[str, str]:
    """Wrap one REG-C error as a MAN-V501 ValidationReport entry (§6.3)."""
    return {
        "code": "MAN-V501",
        "scope": "schema",
        "path": f"{subject}#{reg_error['path']}",
        "message": f"{reg_error['code']}: {reg_error['message']}",
    }
