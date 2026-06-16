"""Flow 1 — schema derivation + registration at manifest publication (Phase 3).

``register_derived_schemas`` is the registry seam the catalog publish transaction
calls (schema-registry §5.1; plugin-arch §10.3). It runs **inside the caller's
open transaction** — either the manifest version becomes ``published`` *and* every
registry write commits, or neither (R-DER). Per derived subject of manifest ``M``
of scenario ``S`` (``registry.infra.derive``):

* **Subject absent** → create the subject row (ownership per §2.3) and register
  version 1 with every property required (R-DER-3), ``derived_from_definition = M``,
  ``compat_checked_against = NULL``.
* **Subject exists, candidate fingerprint = latest** → register nothing (R-DER-4).
* **Subject exists, changed** → run the §6 ``BACKWARD_ADDITIVE`` gate against the
  latest version; pass → register ``latest + 1``; fail → raise
  :class:`SchemaCompatibilityError` carrying one error per REG-C violation. The
  caller (catalog publish) maps this to a ``422 manifest-validation-failed`` with
  one ``MAN-V501`` per violation, ``scope: "schema"`` (§6.3, fail at the manifest).

Race-free version assignment: the subject row is locked
(``SELECT … FOR UPDATE``) before reading its latest version, so ``latest + 1``
cannot be assigned twice (§3.3). Each registered version emits the audited action
``registry.schema_version.registered`` (INV-AUD-2; via the caller's ``record_audit``
seam passed in as ``on_registered`` so this app imports no other app's writer).

Service layer: owns the registry write orchestration; the derivation/compat logic
is in ``registry.infra``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from registry.domain.models import COMPAT_BACKWARD_ADDITIVE, SchemaVersion, Subject
from registry.infra.canonical import fingerprint
from registry.infra.compat import CompatError, check_backward_additive
from registry.infra.derive import DerivedSubject, derive_subjects, document_for_version


class OnRegistered(Protocol):
    """Callback invoked once per newly registered version (the audit seam)."""

    def __call__(self, *, subject: str, version: int, workspace_id: UUID | None) -> None: ...


@dataclass(frozen=True)
class RegisteredVersion:
    """Outcome of registering (or no-op'ing) one derived subject."""

    subject: str
    version: int
    created: bool  # False ⇒ R-DER-4 no-op (mapped to existing latest)


class SchemaCompatibilityError(Exception):
    """A derived schema is not BACKWARD_ADDITIVE-compatible (INV-REG-3).

    Carries the §6.3 ``{code, path, message}`` errors, each prefixed with the
    subject so the caller can build per-subject MAN-V501 entries.
    """

    def __init__(self, subject: str, errors: list[CompatError]) -> None:
        self.subject = subject
        self.errors = errors
        super().__init__(f"{subject}: {len(errors)} BACKWARD_ADDITIVE violation(s)")


def register_derived_schemas(
    manifest: dict[str, Any],
    *,
    scenario_id: UUID,
    workspace_id: UUID | None,
    definition_id: UUID,
    on_registered: OnRegistered | None = None,
) -> list[RegisteredVersion]:
    """Derive + register every subject of ``manifest`` in the caller's transaction.

    ``workspace_id`` is ``None`` for global/builtin scenarios (subjects own a NULL
    workspace), set for tenant scenarios. Raises :class:`SchemaCompatibilityError`
    on the first non-additive subject (the publish transaction then rolls back).
    """
    results: list[RegisteredVersion] = []
    for derived in derive_subjects(manifest):
        result = _register_one(
            derived=derived,
            scenario_id=scenario_id,
            workspace_id=workspace_id,
            definition_id=definition_id,
            on_registered=on_registered,
        )
        results.append(result)
    return results


def _register_one(
    *,
    derived: DerivedSubject,
    scenario_id: UUID,
    workspace_id: UUID | None,
    definition_id: UUID,
    on_registered: OnRegistered | None,
) -> RegisteredVersion:
    subject_name = derived.subject
    subject_row = _lock_subject(subject_name, workspace_id)

    if subject_row is None:
        # Subject absent → create + register version 1 with every property required
        # (R-DER-3). ``derived.document`` is the all-required v1 closed document.
        subject_row = Subject.objects.create(
            subject=subject_name,
            scenario_id=scenario_id,
            workspace_id=workspace_id,
            compatibility_mode=COMPAT_BACKWARD_ADDITIVE,
        )
        return _create_version(
            subject_row=subject_row,
            document=derived.document,
            fingerprint_hex=fingerprint(derived.document),
            version=1,
            compat_checked_against=None,
            workspace_id=workspace_id,
            definition_id=definition_id,
            on_registered=on_registered,
        )

    latest = (
        SchemaVersion.objects.filter(subject=subject_row).order_by("-version").first()
    )
    if latest is None:  # pragma: no cover - a subject row always has version 1
        return _create_version(
            subject_row=subject_row,
            document=derived.document,
            fingerprint_hex=fingerprint(derived.document),
            version=1,
            compat_checked_against=None,
            workspace_id=workspace_id,
            definition_id=definition_id,
            on_registered=on_registered,
        )

    # Subject exists → the candidate that *would* be registered is the version-N
    # document under REQ-RULE: required carried forward from the latest exactly,
    # newly-added properties optional + ``x-df-binding``-annotated (§4.1/§5.1/§5.3).
    candidate = document_for_version(
        derived,
        latest_required=list(latest.json_schema.get("required", []) or []),
        latest_properties=dict(latest.json_schema.get("properties", {}) or {}),
        next_version=latest.version + 1,
    )
    candidate_fp = fingerprint(candidate)

    # candidate fingerprint = latest fingerprint → register nothing (R-DER-4). The
    # fingerprint is over the comparison form, so an unchanged subject (carrying the
    # same required set + properties) re-derives byte-identically and no-ops.
    if latest.fingerprint == candidate_fp:
        return RegisteredVersion(subject=subject_name, version=latest.version, created=False)

    errors = check_backward_additive(latest.json_schema, candidate)
    if errors:
        raise SchemaCompatibilityError(subject_name, errors)

    return _create_version(
        subject_row=subject_row,
        document=candidate,
        fingerprint_hex=candidate_fp,
        version=latest.version + 1,
        compat_checked_against=latest.version,
        workspace_id=workspace_id,
        definition_id=definition_id,
        on_registered=on_registered,
    )


def _lock_subject(subject_name: str, workspace_id: UUID | None) -> Subject | None:
    """SELECT … FOR UPDATE the subject row (§3.3 race-free version assignment).

    The registry tables are hybrid (not Class T scoped managers) so ``objects`` is
    a plain manager; RLS Class H + the explicit ``workspace_id`` filter carry
    isolation (a global subject keys on ``workspace_id IS NULL``, a tenant subject
    on the active workspace).
    """
    qs = Subject.objects.select_for_update().filter(subject=subject_name)
    qs = qs.filter(workspace_id__isnull=True) if workspace_id is None else qs.filter(
        workspace_id=workspace_id
    )
    return qs.first()


def _create_version(
    *,
    subject_row: Subject,
    document: dict[str, Any],
    fingerprint_hex: str,
    version: int,
    compat_checked_against: int | None,
    workspace_id: UUID | None,
    definition_id: UUID,
    on_registered: OnRegistered | None,
) -> RegisteredVersion:
    SchemaVersion.objects.create(
        subject=subject_row,
        workspace_id=workspace_id,
        version=version,
        json_schema=document,
        fingerprint=fingerprint_hex,
        compat_checked_against=compat_checked_against,
        derived_from_definition=definition_id,
    )
    if on_registered is not None:
        on_registered(
            subject=subject_row.subject, version=version, workspace_id=workspace_id
        )
    return RegisteredVersion(subject=subject_row.subject, version=version, created=True)
