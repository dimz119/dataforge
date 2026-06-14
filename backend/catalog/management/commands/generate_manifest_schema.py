"""``manage.py generate_manifest_schema`` — write the Manifest v0 JSON Schema artifact.

Generates the machine-readable freeze of scenario-plugin-architecture §9.1
(``dataforge_engine.manifest.generate_manifest_schema``) and writes it to
``backend/catalog/schema/manifest-v0.schema.json``. The artifact is committed and
contract-tested against §9.1 (ADR-0001); CI runs this command with ``--check`` to
fail the build if the committed file drifts from the generator — the same
artifact-diff discipline the envelope and OpenAPI artifacts use.

The schema itself is produced by the framework-free engine package; this command
is the thin Django wrapper that serialises it deterministically (2-space indent,
``ensure_ascii=False``, trailing newline) and writes / diffs the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from dataforge_engine.manifest import generate_manifest_schema

# Committed under the catalog app so the schema ships beside the validator that
# enforces it (the assignment's deliverable path).
_ARTIFACT_PATH = Path(__file__).resolve().parents[2] / "schema" / "manifest-v0.schema.json"


def _render() -> str:
    """Deterministic JSON text for the artifact (byte-stable across runs)."""
    return json.dumps(generate_manifest_schema(), indent=2, ensure_ascii=False) + "\n"


class Command(BaseCommand):
    help = (
        "Generate (or --check) the Manifest v0 JSON Schema CI artifact at "
        "catalog/schema/manifest-v0.schema.json (scenario-plugin-architecture §9.1)."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Verify the committed artifact matches the generator; exit non-zero on drift.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rendered = _render()
        if options["check"]:
            if not _ARTIFACT_PATH.exists():
                raise CommandError(
                    f"{_ARTIFACT_PATH} is missing — run "
                    "`manage.py generate_manifest_schema` and commit it."
                )
            if _ARTIFACT_PATH.read_text(encoding="utf-8") != rendered:
                raise CommandError(
                    f"{_ARTIFACT_PATH} is out of date — regenerate with "
                    "`manage.py generate_manifest_schema` and commit (artifact-diff gate)."
                )
            self.stdout.write(self.style.SUCCESS(f"{_ARTIFACT_PATH} is up to date."))
            return

        _ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ARTIFACT_PATH.write_text(rendered, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote {_ARTIFACT_PATH}"))
