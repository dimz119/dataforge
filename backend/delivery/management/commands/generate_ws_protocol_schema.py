"""``manage.py generate_ws_protocol_schema`` — write the WS frame JSON Schema artifact.

Generates the machine-readable freeze of the ``dataforge.events.v1`` frame catalog
(delivery-channels §6.3; api-spec §5.5) from
``delivery.domain.ws_protocol.generate_ws_protocol_schema`` and writes it to
``backend/schema/ws-protocol-v1.schema.json`` — beside ``envelope-1.0.schema.json``
and ``openapi.yaml`` in the committed contract set (api-spec T-3). CI runs this
command with ``--check`` to fail the build if the committed file drifts from the
generator, the same artifact-diff discipline the envelope/manifest/OpenAPI artifacts
use (ADR-0001). WS frames are out of OpenAPI scope by design (T-7); their schema ships
here and is exercised by the cross-channel contract suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from delivery.domain.ws_protocol import generate_ws_protocol_schema

# Committed under backend/schema/ (the shared contract set, FS-3).
_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[3] / "schema" / "ws-protocol-v1.schema.json"
)


def _render() -> str:
    """Deterministic JSON text for the artifact (byte-stable across runs)."""
    return json.dumps(generate_ws_protocol_schema(), indent=2, ensure_ascii=False) + "\n"


class Command(BaseCommand):
    help = (
        "Generate (or --check) the WS protocol v1 JSON Schema CI artifact at "
        "schema/ws-protocol-v1.schema.json (delivery-channels §6.3; api-spec §5.5)."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail (non-zero) if the committed artifact differs from the generator.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        rendered = _render()
        if options["check"]:
            if not _ARTIFACT_PATH.exists():
                raise CommandError(f"Missing WS schema artifact: {_ARTIFACT_PATH}")
            current = _ARTIFACT_PATH.read_text(encoding="utf-8")
            if current != rendered:
                raise CommandError(
                    "WS protocol schema artifact is stale; run "
                    "`manage.py generate_ws_protocol_schema` and commit the diff."
                )
            self.stdout.write("ws-protocol-v1.schema.json is up to date.")
            return
        _ARTIFACT_PATH.write_text(rendered, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {_ARTIFACT_PATH}"))
