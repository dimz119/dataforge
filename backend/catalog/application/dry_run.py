"""Catalog-side facade over the Layer-3 dry-run host (plugin-arch §8.4).

Layer 3 is the only validation layer that observes *realized* behaviour: it
executes the **actual generic runtime** (the same engine the runner and golden
replay use, behavior-engine §1) over the candidate manifest in the §8.4 sandbox —
fixed published seed ``424242424242``, ``min(default, 1000)`` catalogs, chaos
disabled, intensity flat, backfill-style unpaced execution, bounded by 30 s wall /
256 MiB RSS / 50,000 events / 1,000 completed traversals — with a throwaway
in-memory ledger and no-op pool store (no real DB writes). It detects what static
L1+L2 cannot: near-absorbing stay loops yielding too-few-events (MAN-D601),
guard-induced livelock that V205/V207 could not see (MAN-D602), value-realization
faults (MAN-D603), a throughput floor breach (MAN-D604), and an oversized realized
payload (MAN-D605); plus the W-D610..612 warnings.

This module is the catalog app's thin seam onto the engine host
(``dataforge_engine.behavior.run_dry_run``): the engine returns a pure
:class:`~dataforge_engine.behavior.DryRunResult`; this module merges it into the
persisted §8.3 :class:`ValidationReport` the catalog stores on a ManifestVersion
(``validation_report`` JSONB) so the existing validation-report endpoint surfaces
the ``dry_run`` block + the MAN-D/W findings.

Sequencing (§8.4): L3 runs only *after* L1+L2 pass — a structurally invalid
document is rejected before any execution. The catalog drives L3 as a Celery
``validation``-queue job (``catalog.tasks``) on builtin sync; the persisted report
is polled via the existing endpoint.

Service layer: imports the engine (a root package — permitted) and the engine's
``ValidationReport`` shape; no infra/DB coupling here (the task owns persistence).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.behavior import DryRunResult, run_dry_run
from dataforge_engine.manifest import (
    ValidationError,
    ValidationReport,
    ValidationWarning,
)

__all__ = [
    "DryRunResult",
    "merge_dry_run_into_report",
    "run_layer3_dry_run",
    "run_layer3_on_report",
]


def run_layer3_dry_run(document: dict[str, Any]) -> DryRunResult:
    """Execute the §8.4 Layer-3 dry run on an L1/L2-valid candidate manifest.

    A thin pass-through to the engine host. Never raises for a manifest fault — a
    runtime fault the dry run is designed to catch (livelock, value realization)
    is converted to the matching MAN-D code in the returned result.
    """
    return run_dry_run(document)


def merge_dry_run_into_report(
    base: ValidationReport, result: DryRunResult
) -> ValidationReport:
    """Fold a :class:`DryRunResult` into a passing L1+L2 :class:`ValidationReport`.

    Produces the §8.3 report the catalog persists: the L3 errors (MAN-D601..605)
    are appended to ``errors`` and the L3 warnings (W-D610..612) to ``warnings``;
    the realized metrics become the ``dry_run`` block; ``status`` is downgraded to
    ``"failed"`` if any MAN-D error fired (L1+L2 had already passed, INV-CAT-2).

    The dry-run errors/warnings carry ``scope="manifest"`` — L3 runs on the
    manifest document, never an overlay (overlays cannot change structure, §11.1).
    """
    l3_errors = tuple(
        ValidationError(
            code=code, path=path, message=message,
            bound=bound, actual=actual, scope="manifest",
        )
        for (code, path, message, bound, actual) in result.errors
    )
    l3_warnings = tuple(
        ValidationWarning(code=code, path=path, message=message)
        for (code, path, message) in result.warnings
    )
    errors = base.errors + l3_errors
    return ValidationReport(
        status="failed" if errors else "passed",
        schema_version=base.schema_version,
        errors=errors,
        warnings=base.warnings + l3_warnings,
        dry_run=result.metrics or None,
    )


def run_layer3_on_report(
    document: dict[str, Any], base_report: dict[str, Any]
) -> dict[str, Any]:
    """Run L3 and return the merged §8.3 report dict (the persistence shape).

    ``base_report`` is the persisted L1+L2 report dict (the ``validation_report``
    JSONB). L3 runs only when that report has already passed L1+L2 (§8.4
    sequencing); a non-passing base is returned unchanged (L3 has nothing to add
    to a structurally invalid document — its errors stand on their own).
    """
    if base_report.get("status") != "passed":
        return base_report
    base = _report_from_dict(base_report)
    result = run_layer3_dry_run(document)
    return merge_dry_run_into_report(base, result).to_dict()


def _report_from_dict(report: dict[str, Any]) -> ValidationReport:
    """Reconstruct a :class:`ValidationReport` from its persisted dict shape.

    Used to fold L3 findings into an already-persisted L1+L2 report without
    re-running L1+L2. Tolerant of partially-shaped persisted reports (older rows).
    """
    errors = tuple(
        ValidationError(
            code=str(e.get("code", "")),
            path=str(e.get("path", "")),
            message=str(e.get("message", "")),
            bound=e.get("bound"),
            actual=e.get("actual"),
            scope=e.get("scope", "manifest"),
        )
        for e in report.get("errors", []) or []
    )
    warnings = tuple(
        ValidationWarning(
            code=str(w.get("code", "")),
            path=str(w.get("path", "")),
            message=str(w.get("message", "")),
        )
        for w in report.get("warnings", []) or []
    )
    return ValidationReport(
        status="passed" if not errors else "failed",
        schema_version=str(report.get("schema_version", "v0")),
        errors=errors,
        warnings=warnings,
        dry_run=report.get("dry_run"),
    )
