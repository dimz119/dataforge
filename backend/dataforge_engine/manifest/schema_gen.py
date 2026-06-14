"""Generator for the Manifest ``v0`` JSON Schema (CI artifact; ADR-0001).

The schema is the machine-readable freeze of scenario-plugin-architecture §9.1 —
the grammar version ``v0``, frozen at Phase 0. It is built here as a plain dict
(single source of truth) and written to
``backend/catalog/schema/manifest-v0.schema.json`` by a management command, then
golden-fixture-tested against §9.1 so the document and the artifact can never
silently drift (the same artifact-diff discipline as the envelope schema, EV-6).

Layer 1 of the validation pipeline (§8.1) validates a parsed manifest against
this schema; structural bounds (counts, patterns, ranges) live here, per-generator
``params`` constraints are Layer 2 (§4 catalogs). Draft 2020-12.

Pure Python (BE-ENG-1): zero Django / DRF / Celery / redis / psycopg imports
(import-linter contract 2 is CI-blocking).
"""

from __future__ import annotations

from typing import Any

from .generators import GENERATOR_NAMES

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_ID = "https://dataforge.dev/schemas/manifest/v0.json"

# §9.1 patterns, transcribed verbatim from the frozen schema.
_IDENTIFIER = "^[a-z][a-z0-9_]{0,31}$"
_IDENTIFIER64 = "^[a-z][a-z0-9_]{0,63}$"
_SEMVER = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
_DURATION = r"^P(?!$)([0-9]+D)?(T(?=[0-9])([0-9]+H)?([0-9]+M)?([0-9]+(\.[0-9]+)?S)?)?$"
_CONTEXT_PATH = (
    r"^(actor|subject|session|created\.[a-z][a-z0-9_]{0,31})"
    r"(\.[a-z][a-z0-9_]{0,63}(\[\])?){1,3}$"
)
_ENTITY_REF = (
    r"^(actor|subject|created\.[a-z][a-z0-9_]{0,31})"
    r"(\.via\.[a-z][a-z0-9_]{0,31}){0,2}$"
)

_OPS_ENUM = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "within"]


def _defs() -> dict[str, Any]:
    """The ``$defs`` block — §9.1 verbatim, ordered as the document declares."""
    return {
        "identifier": {"type": "string", "pattern": _IDENTIFIER},
        "identifier64": {"type": "string", "pattern": _IDENTIFIER64},
        "duration": {"type": "string", "pattern": _DURATION},
        "probability": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "contextPath": {"type": "string", "maxLength": 160, "pattern": _CONTEXT_PATH},
        "entityRef": {"type": "string", "maxLength": 160, "pattern": _ENTITY_REF},
        "generatorSpec": {
            "type": "object",
            "additionalProperties": False,
            "required": ["generator"],
            "properties": {
                "generator": {"enum": list(GENERATOR_NAMES)},
                "params": {"type": "object", "maxProperties": 16},
            },
        },
        "valueSource": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "from": {"$ref": "#/$defs/contextPath"},
                "const": {"type": ["string", "number", "boolean"]},
                "generated": {"$ref": "#/$defs/generatorSpec"},
                "nullable": {"type": "boolean", "default": False},
            },
            "oneOf": [
                {"required": ["from"]},
                {"required": ["const"]},
                {"required": ["generated"]},
            ],
        },
        "entity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key_prefix", "key_attribute", "attributes"],
            "properties": {
                "description": {"type": "string", "maxLength": 500},
                "key_prefix": {"type": "string", "pattern": "^[a-z]{2,8}$"},
                "key_attribute": {"$ref": "#/$defs/identifier64"},
                "attributes": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 100,
                    "propertyNames": {"pattern": _IDENTIFIER64},
                    "additionalProperties": {"$ref": "#/$defs/generatorSpec"},
                },
            },
        },
        "relationship": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "name",
                "source_entity",
                "source_attribute",
                "target_entity",
                "cardinality",
            ],
            "properties": {
                "name": {"$ref": "#/$defs/identifier"},
                "source_entity": {"$ref": "#/$defs/identifier"},
                "source_attribute": {"$ref": "#/$defs/identifier64"},
                "target_entity": {"$ref": "#/$defs/identifier"},
                "cardinality": {"enum": ["many_to_one", "one_to_one"]},
                "on_target_delete": {
                    "enum": ["restrict", "cascade", "set_null"],
                    "default": "restrict",
                },
                "owned": {"type": "boolean", "default": False},
            },
        },
        "eventType": {
            "type": "object",
            "additionalProperties": False,
            "required": ["payload"],
            "properties": {
                "description": {"type": "string", "maxLength": 500},
                "partition_by": {"$ref": "#/$defs/entityRef"},
                "payload": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 64,
                    "propertyNames": {"pattern": _IDENTIFIER64},
                    "additionalProperties": {"$ref": "#/$defs/valueSource"},
                },
            },
        },
        "distribution": _distribution_def(),
        "comparison": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "op"],
            "properties": {
                "path": {"$ref": "#/$defs/contextPath"},
                "op": {"enum": list(_OPS_ENUM)},
                "value": {
                    "type": ["string", "number", "boolean", "array"],
                    "items": {"type": ["string", "number", "boolean"]},
                    "maxItems": 50,
                },
            },
        },
        "existsCondition": _exists_condition_def(),
        "guardCondition": {
            "oneOf": [
                {"$ref": "#/$defs/comparison"},
                {"$ref": "#/$defs/existsCondition"},
            ]
        },
        "effect": _effect_def(),
        "transition": _transition_def(),
        "state": _state_def(),
        "stateMachine": _state_machine_def(),
        "cdcConfig": _cdc_config_def(),
        "intensityConfig": _intensity_config_def(),
        "seedingConfig": _seeding_config_def(),
        "chaosMode": {
            "type": "object",
            "additionalProperties": False,
            "required": ["enabled"],
            "properties": {
                "enabled": {"type": "boolean"},
                "rate": {"type": "number", "minimum": 0, "maximum": 0.5},
                "params": {"type": "object", "maxProperties": 8},
            },
        },
        "chaosDefaults": _chaos_defaults_def(),
    }


def _distribution_def() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["family", "value"],
                "properties": {
                    "family": {"const": "fixed"},
                    "value": {"$ref": "#/$defs/duration"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["family", "min", "max"],
                "properties": {
                    "family": {"const": "uniform"},
                    "min": {"$ref": "#/$defs/duration"},
                    "max": {"$ref": "#/$defs/duration"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["family", "median", "p95"],
                "properties": {
                    "family": {"const": "lognormal"},
                    "median": {"$ref": "#/$defs/duration"},
                    "p95": {"$ref": "#/$defs/duration"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["family", "mean"],
                "properties": {
                    "family": {"const": "exponential"},
                    "mean": {"$ref": "#/$defs/duration"},
                },
            },
        ]
    }


def _exists_condition_def() -> dict[str, Any]:
    where_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["attribute", "op"],
        "properties": {
            "attribute": {"$ref": "#/$defs/identifier64"},
            "op": {"enum": list(_OPS_ENUM)},
            "value": {
                "type": ["string", "number", "boolean", "array"],
                "items": {"type": ["string", "number", "boolean"]},
                "maxItems": 50,
            },
            "ref": {"$ref": "#/$defs/contextPath"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["exists"],
        "properties": {
            "exists": {
                "type": "object",
                "additionalProperties": False,
                "required": ["relationship", "of"],
                "properties": {
                    "relationship": {"$ref": "#/$defs/identifier"},
                    "of": {"$ref": "#/$defs/entityRef"},
                    "negate": {"type": "boolean", "default": False},
                    "where": {"type": "array", "maxItems": 4, "items": where_item},
                },
            }
        },
    }


def _effect_def() -> dict[str, Any]:
    value_map = {"$ref": "#/$defs/valueSource"}
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "entity"],
                "properties": {
                    "action": {"const": "create"},
                    "entity": {"$ref": "#/$defs/identifier"},
                    "set": {
                        "type": "object",
                        "maxProperties": 100,
                        "additionalProperties": value_map,
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "target", "set"],
                "properties": {
                    "action": {"const": "update"},
                    "target": {"$ref": "#/$defs/entityRef"},
                    "set": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 100,
                        "additionalProperties": value_map,
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "target", "attribute", "by"],
                "properties": {
                    "action": {"const": "adjust"},
                    "target": {"$ref": "#/$defs/entityRef"},
                    "attribute": {"$ref": "#/$defs/identifier64"},
                    "by": {
                        "oneOf": [
                            {"type": "number"},
                            {"$ref": "#/$defs/contextPath"},
                        ]
                    },
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "target"],
                "properties": {
                    "action": {"const": "delete"},
                    "target": {"$ref": "#/$defs/entityRef"},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "key", "mode", "value"],
                "properties": {
                    "action": {"const": "remember"},
                    "key": {"$ref": "#/$defs/identifier"},
                    "mode": {"enum": ["set", "append"]},
                    "value": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": 16,
                        "additionalProperties": value_map,
                    },
                },
            },
        ]
    }


def _transition_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["to", "probability"],
        "properties": {
            "to": {"$ref": "#/$defs/identifier"},
            "probability": {"$ref": "#/$defs/probability"},
            "dwell": {"$ref": "#/$defs/distribution"},
            "guard": {
                "type": "object",
                "additionalProperties": False,
                "required": ["all"],
                "properties": {
                    "all": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "items": {"$ref": "#/$defs/guardCondition"},
                    }
                },
            },
            "effects": {
                "type": "array",
                "maxItems": 8,
                "items": {"$ref": "#/$defs/effect"},
            },
            "emit": {"$ref": "#/$defs/identifier64"},
            "override": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "allowed": {"type": "boolean", "default": True},
                    "min": {"type": "number", "minimum": 0, "maximum": 1},
                    "max": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    }


def _state_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "terminal": {"type": "boolean", "default": False},
            "remainder": {"enum": ["exit", "stay"]},
            "timeout": {
                "type": "object",
                "additionalProperties": False,
                "required": ["after", "to"],
                "properties": {
                    "after": {"$ref": "#/$defs/duration"},
                    "to": {"$ref": "#/$defs/identifier"},
                    "emit": {"$ref": "#/$defs/identifier64"},
                },
            },
            "transitions": {
                "type": "array",
                "maxItems": 20,
                "items": {"$ref": "#/$defs/transition"},
            },
        },
    }


def _state_machine_def() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "binds", "initial", "states"],
        "properties": {
            "type": {"enum": ["session", "lifecycle"]},
            "binds": {"$ref": "#/$defs/identifier"},
            "initial": {"$ref": "#/$defs/identifier"},
            "session_timeout": {"$ref": "#/$defs/duration"},
            "states": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 40,
                "propertyNames": {"pattern": _IDENTIFIER},
                "additionalProperties": {"$ref": "#/$defs/state"},
            },
        },
    }


def _cdc_config_def() -> dict[str, Any]:
    background_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "rate", "set"],
        "properties": {
            "name": {"$ref": "#/$defs/identifier"},
            "rate": {
                "type": "object",
                "additionalProperties": False,
                "required": ["per", "probability"],
                "properties": {
                    "per": {"const": "entity_day"},
                    "probability": {"$ref": "#/$defs/probability"},
                },
            },
            "set": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 16,
                "additionalProperties": {"$ref": "#/$defs/generatorSpec"},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["entities"],
        "properties": {
            "entities": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 50,
                "propertyNames": {"pattern": _IDENTIFIER},
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "enabled_default": {"type": "boolean", "default": False},
                        "ops": {
                            "type": "array",
                            "items": {"enum": ["c", "u", "d"]},
                            "uniqueItems": True,
                            "minItems": 1,
                            "default": ["c", "u", "d"],
                        },
                        "background_mutations": {
                            "type": "array",
                            "maxItems": 8,
                            "items": background_item,
                        },
                    },
                },
            }
        },
    }


def _intensity_config_def() -> dict[str, Any]:
    weekday = {"type": "number", "minimum": 0, "maximum": 10}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "diurnal": {
                "type": "array",
                "minItems": 1,
                "maxItems": 24,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from_hour", "to_hour", "multiplier"],
                    "properties": {
                        "from_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                        "to_hour": {"type": "integer", "minimum": 1, "maximum": 24},
                        "multiplier": {"type": "number", "minimum": 0, "maximum": 10},
                    },
                },
            },
            "weekly": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "properties": {
                    "mon": weekday,
                    "tue": weekday,
                    "wed": weekday,
                    "thu": weekday,
                    "fri": weekday,
                    "sat": weekday,
                    "sun": weekday,
                },
            },
        },
    }


def _seeding_config_def() -> dict[str, Any]:
    count = {"type": "integer", "minimum": 0, "maximum": 100000}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["catalogs"],
        "properties": {
            "catalogs": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 50,
                "propertyNames": {"pattern": _IDENTIFIER},
                "additionalProperties": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["default"],
                    "properties": {"default": count, "min": count, "max": count},
                },
            }
        },
    }


def _chaos_defaults_def() -> dict[str, Any]:
    mode = {"$ref": "#/$defs/chaosMode"}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "duplicates": mode,
            "late_arriving": mode,
            "missing": mode,
            "out_of_order": mode,
            "corrupted_values": mode,
            "nulls": mode,
            "schema_drift": mode,
        },
    }


def generate_manifest_schema() -> dict[str, Any]:
    """Build the Manifest ``v0`` JSON Schema as a plain dict (deterministic).

    Mirrors scenario-plugin-architecture §9.1 exactly: required top-level
    sections, ``additionalProperties: false``, the §9.1 ``$defs`` block. The
    generator enum is sourced from ``generators.GENERATOR_NAMES`` (the single
    closed allowlist, also consumed by Layer 2 and Phase 4). Re-running yields a
    byte-identical artifact (the writer does not sort keys).
    """
    return {
        "$schema": _SCHEMA_DIALECT,
        "$id": _SCHEMA_ID,
        "title": "DataForge scenario manifest, grammar v0",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "manifest_schema",
            "metadata",
            "entities",
            "event_types",
            "state_machines",
            "seeding",
        ],
        "properties": {
            "manifest_schema": {"const": "v0"},
            "metadata": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slug", "version", "title", "actor_entity"],
                "properties": {
                    "slug": {"$ref": "#/$defs/identifier"},
                    "version": {"type": "string", "pattern": _SEMVER},
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "description": {"type": "string", "maxLength": 2000},
                    "actor_entity": {"$ref": "#/$defs/identifier"},
                    "simulated_timezone": {
                        "type": "string",
                        "maxLength": 64,
                        "default": "UTC",
                    },
                },
            },
            "entities": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 50,
                "propertyNames": {"pattern": _IDENTIFIER},
                "additionalProperties": {"$ref": "#/$defs/entity"},
            },
            "relationships": {
                "type": "array",
                "maxItems": 100,
                "items": {"$ref": "#/$defs/relationship"},
            },
            "event_types": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 200,
                "propertyNames": {"pattern": _IDENTIFIER64},
                "additionalProperties": {"$ref": "#/$defs/eventType"},
            },
            "state_machines": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 10,
                "propertyNames": {"pattern": _IDENTIFIER},
                "additionalProperties": {"$ref": "#/$defs/stateMachine"},
            },
            "cdc": {"$ref": "#/$defs/cdcConfig"},
            "intensity": {"$ref": "#/$defs/intensityConfig"},
            "seeding": {"$ref": "#/$defs/seedingConfig"},
            "chaos_defaults": {"$ref": "#/$defs/chaosDefaults"},
        },
        "$defs": _defs(),
    }
