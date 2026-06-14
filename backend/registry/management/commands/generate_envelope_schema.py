"""``manage.py generate_envelope_schema`` — write the envelope 1.0 JSON Schema CI artifact.

Generates the machine-readable freeze of the §2.1 field catalog
(``dataforge_engine.envelope.generate_envelope_schema``) and writes it to
``backend/schema/envelope-1.0.schema.json``. The artifact is committed and
golden-fixture-tested against event-model §2.1 (EV-6); CI runs this command with
``--check`` to fail the build if the committed file drifts from the generator —
the same artifact-diff discipline the OpenAPI artifact uses.

The schema itself is produced by the framework-free engine package; this command
is the thin Django wrapper that serialises it deterministically (2-space indent,
``ensure_ascii=False``, trailing newline) and writes / diffs the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dataforge_engine.envelope import generate_envelope_schema

# Path of the committed artifact, relative to the backend root (the dir holding
# ``manage.py``). ``settings.BASE_DIR`` points at the backend project root.
_ARTIFACT_RELPATH = Path("schema") / "envelope-1.0.schema.json"


def _render() -> str:
    """Deterministic JSON text for the artifact (byte-stable across runs)."""
    schema = generate_envelope_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def _artifact_path() -> Path:
    return Path(settings.BASE_DIR) / _ARTIFACT_RELPATH


class Command(BaseCommand):
    help = (
        "Generate (or --check) the envelope 1.0 JSON Schema CI artifact at "
        "schema/envelope-1.0.schema.json (event-model EV-6)."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Verify the committed artifact matches the generator; exit non-zero on drift.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = _artifact_path()
        rendered = _render()

        if options["check"]:
            if not path.exists():
                raise CommandError(
                    f"{path} is missing — run `manage.py generate_envelope_schema` and commit it."
                )
            current = path.read_text(encoding="utf-8")
            if current != rendered:
                raise CommandError(
                    f"{path} is out of date — regenerate with "
                    "`manage.py generate_envelope_schema` and commit (EV-6 artifact-diff gate)."
                )
            self.stdout.write(self.style.SUCCESS(f"{path} is up to date."))
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"wrote {path}"))
