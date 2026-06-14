"""Generator for the envelope ``1.0`` JSON Schema (CI artifact; event-model EV-6).

The schema is the machine-readable freeze of the §2.1 field catalog. It is
generated here (the single source of truth is the same field order this package
serializes with) and written to ``backend/schema/envelope-1.0.schema.json`` by a
management command, then golden-fixture-tested against §2.1 so the document and
the artifact can never silently drift.

What the schema validates is the *envelope frame* (the 20 fields + their frozen
types/bounds), not the domain shape of ``payload`` — that is registry-versioned
per subject (event-model §2.1 field 20, EV-7). It does pin the Debezium CDC
sub-envelope frame (§4) as a conditional applied when ``op`` is non-null, since
that frame is part of the envelope contract frozen in this document.

Draft 2020-12. ``additionalProperties: false`` at the envelope level with every
field ``required`` realises §2.1's "every delivered event carries all 20 keys"
and the closed-document discipline. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any

from .types import DELIVERED_FIELD_ORDER, ENVELOPE_VERSION

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_ID = "https://dataforge.dev/schema/envelope-1.0.schema.json"

# A lowercase UUID (RFC 9562 canonical form). event_id is UUIDv7 but the schema
# validates the canonical UUID shape; v7-ness is a generation invariant, not a
# wire-validatable constraint of the frame.
_UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# RFC 3339 UTC, exactly 6 fractional digits, literal Z (event-model §2.1 fields 12/13).
_RFC3339_6_Z_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
# Business snake_case OR the reserved cdc.{entity} form (event-model §2.1 field 8).
_EVENT_TYPE_PATTERN = r"^(cdc\.[a-z][a-z0-9_]{0,63}|[a-z][a-z0-9_]{0,63})$"
_SCENARIO_SLUG_PATTERN = r"^[a-z][a-z0-9_]*$"
_SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"

# JS double-safe integer ceiling (event-model S-1).
_JS_MAX_SAFE_INTEGER = 2**53 - 1

_OP_ENUM = ["c", "u", "d", "r"]


def _entity_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "entity_type": {"type": "string"},
            "entity_key": {"type": "string", "maxLength": 64},
        },
        "required": ["entity_type", "entity_key"],
        "additionalProperties": False,
    }


def _cdc_source_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "version": {"const": ENVELOPE_VERSION},
            "connector": {"const": "dataforge"},
            "name": {"type": "string"},
            "ts_ms": {"type": "integer", "minimum": 0, "maximum": _JS_MAX_SAFE_INTEGER},
            "snapshot": {"enum": ["true", "false", "last"]},
            "db": {"type": "string"},
            "table": {"type": "string"},
            "seq": {"type": "integer", "minimum": 1, "maximum": _JS_MAX_SAFE_INTEGER},
            "entity_version": {"type": "integer", "minimum": 1, "maximum": _JS_MAX_SAFE_INTEGER},
            "tx_id": {"type": ["string", "null"], "pattern": _UUID_PATTERN},
        },
        "required": [
            "version", "connector", "name", "ts_ms", "snapshot",
            "db", "table", "seq", "entity_version", "tx_id",
        ],
        "additionalProperties": False,
    }


def _cdc_payload_schema() -> dict[str, Any]:
    """The Debezium-shaped CDC sub-envelope frame (event-model §4.2).

    Row images (``before``/``after``) are objects whose interior shape is
    registry-versioned, so they are ``type: object`` (open) here; the frame
    around them is frozen and closed.
    """
    return {
        "type": "object",
        "properties": {
            "before": {"type": ["object", "null"]},
            "after": {"type": ["object", "null"]},
            "op": {"enum": _OP_ENUM},
            "ts_ms": {"type": "integer", "minimum": 0, "maximum": _JS_MAX_SAFE_INTEGER},
            "source": _cdc_source_schema(),
        },
        "required": ["before", "after", "op", "ts_ms", "source"],
        "additionalProperties": False,
    }


def _field_property_schemas() -> dict[str, dict[str, Any]]:
    """One JSON Schema fragment per envelope field, keyed by field name.

    Mirrors the §2.1 catalog: nullability is ``["T","null"]``; bounds and patterns
    are the frozen ones. ``payload`` is ``type: object`` (the frame validates the
    envelope, not the domain payload — EV-7); the CDC frame is enforced
    conditionally on ``op`` at the schema root.
    """
    return {
        "envelope_version": {"const": ENVELOPE_VERSION},
        "event_id": {"type": "string", "pattern": _UUID_PATTERN},
        "workspace_id": {"type": "string", "pattern": _UUID_PATTERN},
        "stream_id": {"type": "string", "pattern": _UUID_PATTERN},
        "shard_id": {"type": "integer", "minimum": 0},
        "scenario_slug": {"type": "string", "pattern": _SCENARIO_SLUG_PATTERN, "maxLength": 32},
        "manifest_version": {"type": "string", "pattern": _SEMVER_PATTERN},
        "event_type": {"type": "string", "pattern": _EVENT_TYPE_PATTERN, "maxLength": 64},
        "schema_ref": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
            },
            "required": ["subject", "version"],
            "additionalProperties": False,
        },
        "sequence_no": {"type": "integer", "minimum": 1, "maximum": _JS_MAX_SAFE_INTEGER},
        "partition_key": {"type": "string", "maxLength": 256},
        "occurred_at": {"type": "string", "pattern": _RFC3339_6_Z_PATTERN},
        "emitted_at": {"type": "string", "pattern": _RFC3339_6_Z_PATTERN},
        "actor_id": {"type": ["string", "null"]},
        "session_id": {"type": ["string", "null"], "pattern": _UUID_PATTERN},
        "entity_refs": {"type": "array", "minItems": 1, "items": _entity_ref_schema()},
        "correlation_id": {"type": "string", "pattern": _UUID_PATTERN},
        "causation_id": {"type": ["string", "null"], "pattern": _UUID_PATTERN},
        "op": {"type": ["string", "null"], "enum": [*_OP_ENUM, None]},
        "payload": {"type": "object"},
    }


def generate_envelope_schema() -> dict[str, Any]:
    """Build the envelope ``1.0`` JSON Schema as a plain dict (deterministic).

    The property map is keyed in §2.1 catalog order, ``required`` is the exact
    20-field list, ``additionalProperties: false`` closes the document, and a
    conditional ``if op is non-null then payload matches the CDC frame`` pins the
    §4 sub-envelope. Re-running yields a byte-identical artifact (dict order is
    the field order; the writer sorts nothing).
    """
    properties = _field_property_schemas()
    schema: dict[str, Any] = {
        "$schema": _SCHEMA_DIALECT,
        "$id": _SCHEMA_ID,
        "title": "DataForge canonical event envelope 1.0",
        "description": (
            "Frozen envelope contract (event-model §2.1). The frame's 20 fields are "
            "validated here; payload domain shape is registry-versioned per subject."
        ),
        "type": "object",
        "properties": properties,
        "required": list(DELIVERED_FIELD_ORDER),
        "additionalProperties": False,
        "$defs": {"cdc_payload": _cdc_payload_schema()},
        # CDC discriminator (event-model §2.1 field 19, §4.1): when op is one of
        # c/u/d/r, payload must be the Debezium-shaped sub-envelope frame.
        "if": {"properties": {"op": {"enum": _OP_ENUM}}, "required": ["op"]},
        "then": {"properties": {"payload": {"$ref": "#/$defs/cdc_payload"}}},
    }
    return schema
