"""The canonical validation error object and ValidationReport (§8.2/§8.3).

Every Layer-1 and Layer-2 finding is a :class:`ValidationError` with the exact
frozen shape ``{code, path, message, bound, actual, scope}``:

* ``code`` — a ``MAN-S*`` (parse/schema) or ``MAN-V*`` (semantic) code;
* ``path`` — an RFC 6901 JSON Pointer into the **canonical** document (machine-
  actionable, the LLM repair-loop input, AI-2);
* ``message`` — human text that never echoes document content beyond the pointed-at
  values (no prompt-injection amplification, AI-2);
* ``bound`` / ``actual`` — the limit and the observed value when the error is a
  bound/count/sum violation (``None`` otherwise);
* ``scope`` — ``"manifest"`` for a manifest document, ``"override"`` for a merged
  overlay re-validation (§11.1).

The :class:`ValidationReport` is the §8.3 shape the catalog persists on the
ManifestVersion. ``dry_run`` is ``None`` in Phase 3 (Layer 3 lands in Phase 4).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["manifest", "override"]
ReportStatus = Literal["passed", "failed"]

# JSON-serialisable scalar for ``bound``/``actual``.
Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class ValidationError:
    """One validation finding — the frozen ``{code, path, message, bound, actual, scope}``."""

    code: str
    path: str
    message: str
    bound: Scalar = None
    actual: Scalar = None
    scope: Scope = "manifest"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the §8.3 error object (all six keys always present)."""
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "bound": self.bound,
            "actual": self.actual,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ValidationWarning:
    """A non-blocking finding (e.g. a dry-run ``W-D6xx``; reserved for Phase 4)."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class ValidationReport:
    """The §8.3 ValidationReport persisted on a ManifestVersion.

    ``status`` is ``"passed"`` iff ``errors`` is empty. ``schema_version`` echoes
    the manifest grammar version (``"v0"``). ``dry_run`` is ``None`` until Phase 4
    adds Layer 3 (kept in the shape so the persisted JSON is forward-stable).
    """

    status: ReportStatus
    schema_version: str = "v0"
    errors: tuple[ValidationError, ...] = ()
    warnings: tuple[ValidationWarning, ...] = ()
    dry_run: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def codes(self) -> list[str]:
        """The list of error codes, in report order (test/repair-loop helper)."""
        return [e.code for e in self.errors]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the §8.3 JSON shape (the catalog stores this verbatim)."""
        out: dict[str, Any] = {
            "status": self.status,
            "schema_version": self.schema_version,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
        }
        if self.dry_run is not None:
            out["dry_run"] = self.dry_run
        return out


@dataclass
class ErrorCollector:
    """Accumulates :class:`ValidationError` during a validation pass.

    Carries the active ``scope`` so Layer-2 checks emit ``manifest`` or
    ``override`` errors without each call site repeating it.
    """

    scope: Scope = "manifest"
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationWarning] = field(default_factory=list)

    def add(
        self,
        code: str,
        path: str,
        message: str,
        *,
        bound: Scalar = None,
        actual: Scalar = None,
    ) -> None:
        self.errors.append(
            ValidationError(
                code=code,
                path=path,
                message=message,
                bound=bound,
                actual=actual,
                scope=self.scope,
            )
        )

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append(ValidationWarning(code=code, path=path, message=message))

    def has_errors(self) -> bool:
        return bool(self.errors)

    def report(self, *, schema_version: str = "v0") -> ValidationReport:
        return ValidationReport(
            status="failed" if self.errors else "passed",
            schema_version=schema_version,
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
        )


def json_pointer(*segments: str | int) -> str:
    """Build an RFC 6901 JSON Pointer from path segments.

    Escapes ``~`` → ``~0`` and ``/`` → ``~1`` per RFC 6901 §3. ``json_pointer()``
    with no segments is the whole-document pointer ``""``.
    """
    if not segments:
        return ""
    parts = []
    for seg in segments:
        token = str(seg).replace("~", "~0").replace("/", "~1")
        parts.append(token)
    return "/" + "/".join(parts)
