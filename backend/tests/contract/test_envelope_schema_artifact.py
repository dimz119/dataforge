"""Envelope 1.0 JSON Schema artifact contract tests (event-model §2.1, EV-6).

EV-6 pins the schema artifact to the §2.1 field catalog and guards against drift:

* the generator's property set + order == the frozen §2.1 catalog order;
* ``required`` == the exact 20-field list, document closed
  (``additionalProperties: false``);
* the committed artifact on disk == the generator output (the CI artifact-diff
  gate, the same discipline as the OpenAPI artifact);
* the CDC frame (§4) is pinned conditionally on a non-null ``op``.
"""

from __future__ import annotations

import json
from pathlib import Path

from dataforge_engine.envelope import (
    DELIVERED_FIELD_ORDER,
    generate_envelope_schema,
)

_ARTIFACT = Path(__file__).resolve().parents[2] / "schema" / "envelope-1.0.schema.json"

# The frozen §2.1 catalog order, transcribed independently from the document so
# the test fails if either the generator OR the catalog constant drifts.
_CATALOG_ORDER_FROM_DOC = (
    "envelope_version", "event_id", "workspace_id", "stream_id", "shard_id",
    "scenario_slug", "manifest_version", "event_type", "schema_ref", "sequence_no",
    "partition_key", "occurred_at", "emitted_at", "actor_id", "session_id",
    "entity_refs", "correlation_id", "causation_id", "op", "payload",
)


def test_field_order_matches_doc_catalog() -> None:
    assert DELIVERED_FIELD_ORDER == _CATALOG_ORDER_FROM_DOC


def test_generated_schema_properties_match_catalog_order() -> None:
    schema = generate_envelope_schema()
    assert list(schema["properties"].keys()) == list(_CATALOG_ORDER_FROM_DOC)


def test_generated_schema_required_is_exact_20() -> None:
    schema = generate_envelope_schema()
    assert schema["required"] == list(_CATALOG_ORDER_FROM_DOC)
    assert len(schema["required"]) == 20


def test_generated_schema_is_closed() -> None:
    schema = generate_envelope_schema()
    assert schema["additionalProperties"] is False
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"


def test_envelope_version_is_const_1_0() -> None:
    schema = generate_envelope_schema()
    assert schema["properties"]["envelope_version"] == {"const": "1.0"}


def test_op_enum_is_frozen_closed_set() -> None:
    """EV-2: op enum is c/u/d/r plus null, never extended."""
    schema = generate_envelope_schema()
    assert set(schema["properties"]["op"]["enum"]) == {"c", "u", "d", "r", None}


def test_cdc_frame_pinned_conditionally() -> None:
    schema = generate_envelope_schema()
    assert schema["if"]["properties"]["op"]["enum"] == ["c", "u", "d", "r"]
    cdc = schema["$defs"]["cdc_payload"]
    assert cdc["additionalProperties"] is False
    assert set(cdc["required"]) == {"before", "after", "op", "ts_ms", "source"}
    src = cdc["properties"]["source"]
    assert src["properties"]["version"] == {"const": "1.0"}
    assert src["properties"]["connector"] == {"const": "dataforge"}


def test_committed_artifact_matches_generator() -> None:
    """CI artifact-diff gate (EV-6): on-disk file == generator output, byte-for-byte."""
    rendered = json.dumps(generate_envelope_schema(), indent=2, ensure_ascii=False) + "\n"
    assert _ARTIFACT.exists(), "artifact missing — run generate_envelope_schema and commit"
    assert _ARTIFACT.read_text(encoding="utf-8") == rendered, (
        "envelope-1.0.schema.json is stale — regenerate with "
        "`manage.py generate_envelope_schema` and commit."
    )


def test_artifact_is_valid_draft_2020_12() -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(json.loads(_ARTIFACT.read_text(encoding="utf-8")))
