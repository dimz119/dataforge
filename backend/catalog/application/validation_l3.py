"""Layer-3 validation orchestration over a persisted ManifestVersion (plugin-arch §8.4).

The :mod:`catalog.application.dry_run` facade is pure (document in, merged report
out). This module is the DB-touching orchestration the Celery ``validation``-queue
task (``catalog.tasks``) and the builtin-revalidation GUARD command both call: it
loads a :class:`~catalog.domain.models.ManifestVersion`, runs L3 on its stored
canonical document, and persists the merged §8.3 report back onto the row's
``validation_report`` JSONB so the existing validation-report endpoint surfaces the
``dry_run`` block and the MAN-D/W findings.

It runs L3 only when the row's stored L1+L2 report already passed (§8.4
sequencing); a non-passing row is left untouched. Builtins re-validated through this
path power the Phase-4 CI GUARD job (closes the Phase-3 sequencing window): the
builtin subset published in that one-phase window is retroactively re-validated
through L3 and must pass with ``est_eps_per_shard >= 1000`` (MAN-D604).

Idempotent: re-running L3 on the same immutable document re-derives the same
``dry_run`` content (the fixed sandbox seed, §8.4) and overwrites the block;
``est_eps_per_shard`` is a wall-time throughput measurement that varies with the
worker, which is exactly what the MAN-D604 floor gates.

Service layer; persistence is the ORM (catalog.domain.models), the dry run is the
pure facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from catalog.application.dry_run import run_layer3_on_report
from catalog.domain.models import ManifestVersion

__all__ = [
    "L3Outcome",
    "ManifestVersionMissing",
    "run_layer3_for_version",
]


class ManifestVersionMissing(Exception):
    """The targeted ManifestVersion row no longer exists (deleted between enqueue
    and execution); the task logs and drops it (nothing to validate)."""


@dataclass(frozen=True)
class L3Outcome:
    """The outcome of one persisted-row L3 run (the task/command return value)."""

    definition_id: UUID
    ran: bool  # False when L1+L2 had not passed (L3 skipped per §8.4 sequencing)
    passed: bool  # the merged report status == passed
    est_eps_per_shard: int
    report: dict[str, Any]

    @property
    def dry_run_codes(self) -> list[str]:
        """The MAN-D / W-D codes the dry run added (test/GUARD helper)."""
        return [
            e.get("code", "")
            for e in self.report.get("errors", [])
            if str(e.get("code", "")).startswith("MAN-D")
        ]


def run_layer3_for_version(definition_id: UUID) -> L3Outcome:
    """Run L3 on a persisted ManifestVersion and persist the merged §8.3 report.

    Reads the row's stored canonical document + its L1+L2 report, runs the §8.4
    dry run, folds the result into the report, and writes ``validation_report``
    back (a single-column update — the immutable manifest is never touched, so a
    published row's INV-CAT-1 invariant holds: only the report metadata changes).

    Raises :class:`ManifestVersionMissing` if the row is gone.
    """
    version = ManifestVersion.objects.filter(pk=definition_id).first()
    if version is None:
        raise ManifestVersionMissing(str(definition_id))

    base_report: dict[str, Any] = version.validation_report or {}
    if base_report.get("status") != "passed":
        # L1+L2 not passed → L3 has nothing to add (§8.4 sequencing).
        return L3Outcome(
            definition_id=definition_id,
            ran=False,
            passed=base_report.get("status") == "passed",
            est_eps_per_shard=0,
            report=base_report,
        )

    merged = run_layer3_on_report(version.manifest, base_report)
    version.validation_report = merged
    version.save(update_fields=["validation_report"])

    dry_run = merged.get("dry_run") or {}
    return L3Outcome(
        definition_id=definition_id,
        ran=True,
        passed=merged.get("status") == "passed",
        est_eps_per_shard=int(dry_run.get("est_eps_per_shard", 0)),
        report=merged,
    )
