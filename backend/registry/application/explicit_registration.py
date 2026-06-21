"""Flow 2 — explicit registration of curated evolution versions (schema-registry §5.2).

The control-plane write path the ``registry register-version`` command drives
(Phase 10). Unlike Flow 1 (derivation at manifest publish,
``registry.application.registration``), Flow 2 registers a *hand-authored* next
version that no published manifest derives — so the pinned manifest stays untouched
while v1-pinned streams can adopt the version mid-stream through bindings alone
(§10) and drift mode gains injectable fields (§11). This is how the e-commerce
v2/v3 teaching evolutions ship.

It reuses the Flow-1 seams verbatim where they apply — ``_lock_subject`` /
``_create_version`` (race-free ``latest+1`` assignment under ``SELECT … FOR
UPDATE``), ``infra.canonical.fingerprint`` (idempotency + the unique-fingerprint
DB guarantee), and ``infra.compat.check_backward_additive`` (the §6 gate) — and
adds the Flow-2-only checks (§5.2):

* **REG-C011** subject must already exist (subjects are created only by manifest
  publication, §5.1) and **REG-C012** must not be a ``cdc.*`` subject;
* **REG-C008** ``--expected-latest N`` must equal the current latest when supplied;
* **idempotency** candidate fingerprint == *latest* fingerprint → no write, no
  audit (the re-runnable seed); equality with an *older* version is not idempotent
  and fails the gate like any regression;
* **REG-C007** every property added relative to latest must carry an ``x-df-binding``
  whose valueSource resolves against the **latest published manifest** of the
  subject's scenario in every emitting context (``infra.binding`` /
  ``infra.resolve.resolve_from_path``); ``hook.*`` generators are forbidden;
* **caps** ≤ 250 subjects/scenario, ≤ 100 versions/subject (static checks, §5.2).

On success it registers ``latest+1`` with ``derived_from_definition = NULL`` (the
explicit flow has no manifest provenance) and emits ``registry.schema_version.
registered`` (the audit seam passed in, mirroring Flow 1). On any failure it raises
:class:`Flow2Incompatible` carrying the §6.3 Flow-2 report objects, which the
command renders to JSON and exits non-zero.

Service layer: owns the write orchestration; all comparison/binding logic is in
``registry.infra`` (no Django there).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from registry.application.registration import (
    OnRegistered,
    RegisteredVersion,
    _create_version,
    _lock_subject,
)
from registry.domain.models import SUBJECT_PATTERN, SchemaVersion, Subject
from registry.infra import binding as binding_check
from registry.infra.canonical import fingerprint
from registry.infra.compat import CompatError, check_backward_additive

# §5.2 decided caps (refined into the plan-tier quota stack in Phase 11).
MAX_SUBJECTS_PER_SCENARIO = 250
MAX_VERSIONS_PER_SUBJECT = 100

_CDC_MARKER = ".cdc."


@dataclass(frozen=True)
class Flow2Report:
    """The §6.3 Flow-2 JSON report — printed by the command (compatible or not)."""

    subject: str
    latest_version: int | None
    compatible: bool
    errors: list[CompatError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "latest_version": self.latest_version,
            "compatible": self.compatible,
            "errors": [e.to_dict() for e in self.errors],
        }


class Flow2Incompatible(Exception):
    """A Flow-2 candidate failed §4/§6 or a Flow-2-only check (§5.2 catalog).

    Carries the §6.3 report so the command exits non-zero printing it verbatim.
    """

    def __init__(self, report: Flow2Report) -> None:
        self.report = report
        super().__init__(f"{report.subject}: {len(report.errors)} violation(s)")


@dataclass(frozen=True)
class Flow2Outcome:
    """Result of a Flow-2 registration (or a dry-run / idempotent no-op)."""

    report: Flow2Report  # always compatible=True here
    registered: RegisteredVersion | None  # None ⇒ dry-run or idempotent no-op
    idempotent: bool  # candidate == latest fingerprint (no write needed)


def register_explicit_version(
    *,
    subject_name: str,
    candidate: dict[str, Any],
    scenario_id: UUID,
    workspace_id: UUID | None,
    latest_manifest: dict[str, Any],
    expected_latest: int | None = None,
    dry_run: bool = False,
    on_registered: OnRegistered | None = None,
) -> Flow2Outcome:
    """Register ``candidate`` as the subject's next version (Flow 2, §5.2).

    ``latest_manifest`` is the **latest published manifest** of the subject's
    scenario (the binding-validation context for REG-C007 / REG-U005-inputs).
    ``workspace_id`` is ``None`` for global/builtin subjects. Must run inside the
    caller's transaction (the ``FOR UPDATE`` lock makes ``latest+1`` race-free, §3.3).

    Returns a :class:`Flow2Outcome` on success (``registered=None`` for ``dry_run``
    or an idempotent re-run). Raises :class:`Flow2Incompatible` on any violation —
    the command renders ``exc.report`` as the §6.3 JSON and exits non-zero.
    """
    subject_row = _lock_subject(subject_name, workspace_id)

    # REG-C011 / REG-C012 are structural Flow-2 rejections (no latest to gate against).
    structural = _structural_errors(subject_name, subject_row)
    if structural:
        raise Flow2Incompatible(
            Flow2Report(subject_name, latest_version=None, compatible=False, errors=structural)
        )
    assert subject_row is not None  # narrowed by _structural_errors returning []

    latest = (
        SchemaVersion.objects.filter(subject=subject_row).order_by("-version").first()
    )
    latest_version = latest.version if latest is not None else None
    candidate_fp = fingerprint(candidate)

    errors: list[CompatError] = []

    # REG-C008: --expected-latest must equal the current latest when supplied.
    if expected_latest is not None and expected_latest != latest_version:
        errors.append(
            CompatError(
                "REG-C008",
                "/",
                f"subject is at version {latest_version}; re-fetch and retry",
            )
        )

    # Idempotency (§5.2): candidate fingerprint == *latest* fingerprint → no-op,
    # exit 0, no write, no audit (the re-runnable seed). Equality with an older
    # version is NOT idempotent — it falls through to the gate and fails (you cannot
    # re-register v1 on top of v2). Skipped if --expected-latest already mismatched.
    if not errors and latest is not None and latest.fingerprint == candidate_fp:
        return Flow2Outcome(
            report=Flow2Report(subject_name, latest_version, compatible=True),
            registered=None,
            idempotent=True,
        )

    # §6 BACKWARD_ADDITIVE gate against the current latest (latest is non-None: the
    # subject exists ⇒ it has version 1).
    if latest is not None:
        errors.extend(check_backward_additive(latest.json_schema, candidate))
        # REG-C007: every added property must carry a resolvable x-df-binding. Run
        # only when the shape/compat checks found no structural problem at the added
        # property (a malformed fragment is already a C004/C006 error).
        errors.extend(
            binding_check.check_added_bindings(
                latest=latest.json_schema,
                candidate=candidate,
                manifest=latest_manifest,
                subject=subject_name,
            )
        )

    # Caps (§5.2 static checks): a new version would exceed the per-subject limit.
    if latest_version is not None and latest_version >= MAX_VERSIONS_PER_SUBJECT:
        errors.append(
            CompatError(
                "REG-C010",
                "/",
                f"subject has reached the {MAX_VERSIONS_PER_SUBJECT}-version cap",
            )
        )
    errors.extend(_subject_cap_errors(scenario_id, workspace_id))

    if errors:
        raise Flow2Incompatible(
            Flow2Report(subject_name, latest_version, compatible=False, errors=errors)
        )

    next_version = (latest_version or 0) + 1
    if dry_run:
        # --check: §4 + §6 ran clean; report compatible without writing.
        return Flow2Outcome(
            report=Flow2Report(subject_name, latest_version, compatible=True),
            registered=None,
            idempotent=False,
        )

    registered = _create_version(
        subject_row=subject_row,
        document=candidate,
        fingerprint_hex=candidate_fp,
        version=next_version,
        compat_checked_against=latest_version,
        workspace_id=workspace_id,
        definition_id=None,  # Flow 2: derived_from_definition is NULL (explicit)
        on_registered=on_registered,
    )
    return Flow2Outcome(
        report=Flow2Report(subject_name, latest_version, compatible=True),
        registered=registered,
        idempotent=False,
    )


def find_existing_version(
    *, subject_name: str, candidate: dict[str, Any], workspace_id: UUID | None
) -> int | None:
    """The version number of an already-registered identical schema, else ``None``.

    Fingerprint equality ⇔ no schema change (§3.2), so a fixture whose comparison
    form matches an *already-registered* version is already seeded. The seed step
    uses this to stay re-runnable when the subject has advanced **past** the fixture
    (registering an older fixture on top of a newer latest would otherwise fail the
    gate as a regression, §5.2): an intermediate fixture whose version already exists
    is a no-op, not a re-registration. Read-only; no lock needed.
    """
    subject_row = (
        Subject.objects.filter(subject=subject_name)
        .filter(
            workspace_id__isnull=True
        ) if workspace_id is None else Subject.objects.filter(
            subject=subject_name, workspace_id=workspace_id
        )
    ).first()
    if subject_row is None:
        return None
    candidate_fp = fingerprint(candidate)
    match = (
        SchemaVersion.objects.filter(subject=subject_row, fingerprint=candidate_fp)
        .order_by("version")
        .first()
    )
    return match.version if match is not None else None


def _structural_errors(
    subject_name: str, subject_row: Subject | None
) -> list[CompatError]:
    """REG-C012 (cdc.*) then REG-C011 (unknown subject) — the Flow-2-only gates."""
    errors: list[CompatError] = []
    if _CDC_MARKER in subject_name:
        errors.append(
            CompatError(
                "REG-C012",
                "/",
                "CDC row-image schemas evolve only through manifest versions",
            )
        )
    if subject_row is None:
        errors.append(
            CompatError(
                "REG-C011",
                "/",
                "unknown subject; subjects are created by publishing a manifest",
            )
        )
    return errors


def _subject_cap_errors(
    scenario_id: UUID, workspace_id: UUID | None
) -> list[CompatError]:
    """REG-C010: the scenario is already at the ≤250-subjects cap (§5.2).

    Registering a *new version* of an existing subject never adds a subject, so this
    only trips if the scenario is already over the bound — a defensive static check
    that becomes meaningful when paired with the future subject-create paths.
    """
    qs = Subject.objects.filter(scenario_id=scenario_id)
    qs = qs.filter(workspace_id__isnull=True) if workspace_id is None else qs.filter(
        workspace_id=workspace_id
    )
    if qs.count() > MAX_SUBJECTS_PER_SCENARIO:
        return [
            CompatError(
                "REG-C010",
                "/",
                f"scenario has reached the {MAX_SUBJECTS_PER_SCENARIO}-subject cap",
            )
        ]
    return []


def is_valid_subject_name(name: str) -> bool:
    """Cheap structural pre-check used by the command for an early, clear error."""
    import re

    return bool(re.match(SUBJECT_PATTERN, name))
