"""Manifest v0 JSON Schema artifact contract tests (§9.1, ADR-0001).

Pins the committed ``catalog/schema/manifest-v0.schema.json`` to the generator and
to the §9.1 freeze, and guards against drift (the artifact-diff CI gate, the same
discipline as the envelope and OpenAPI artifacts).
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from dataforge_engine.manifest import GENERATOR_NAMES, generate_manifest_schema

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "schema"
    / "manifest-v0.schema.json"
)


def test_artifact_exists() -> None:
    assert _ARTIFACT.exists(), "run `manage.py generate_manifest_schema` and commit"


def test_committed_artifact_matches_generator() -> None:
    rendered = json.dumps(generate_manifest_schema(), indent=2, ensure_ascii=False) + "\n"
    assert _ARTIFACT.read_text(encoding="utf-8") == rendered, (
        "manifest-v0.schema.json is stale — regenerate with "
        "`manage.py generate_manifest_schema` and commit."
    )


def test_artifact_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(json.loads(_ARTIFACT.read_text(encoding="utf-8")))


def test_schema_identity_and_closure() -> None:
    schema = generate_manifest_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "https://dataforge.dev/schemas/manifest/v0.json"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["manifest_schema"] == {"const": "v0"}


def test_required_top_level_sections() -> None:
    schema = generate_manifest_schema()
    assert schema["required"] == [
        "manifest_schema",
        "metadata",
        "entities",
        "event_types",
        "state_machines",
        "seeding",
    ]


def test_generator_enum_is_the_closed_allowlist() -> None:
    schema = generate_manifest_schema()
    enum = schema["$defs"]["generatorSpec"]["properties"]["generator"]["enum"]
    assert enum == list(GENERATOR_NAMES)
    assert enum[0] == "id.uuid"
    assert enum[-1] == "hook"


def test_schema_is_byte_stable_across_runs() -> None:
    first = json.dumps(generate_manifest_schema(), indent=2, ensure_ascii=False)
    second = json.dumps(generate_manifest_schema(), indent=2, ensure_ascii=False)
    assert first == second
