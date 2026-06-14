"""Document builders for the adversarial corpus (testing-strategy §16.3).

Each builder returns one malformed manifest — a ``dict`` mutation of the valid
base (``tests.catalog.fixtures.base.valid_subset_manifest``) or raw text for the
parse-hardening codes (MAN-S001/S002/S003). The builders are deliberately tiny
and self-documenting: the mutation *is* the adversarial intent.

Pure data construction — no Django, no DB.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

from dataforge_engine.manifest import MAX_DOCUMENT_BYTES, MAX_NESTING_DEPTH
from tests.catalog.fixtures.base import valid_subset_manifest

# ---------------------------------------------------------------------------
# Parse-hardening (MAN-S001/S002/S003) — these need raw text, not a dict.
# ---------------------------------------------------------------------------


def yaml_with_alias() -> str:
    """MAN-S001 — a YAML anchor/alias (billion-laughs vector) is rejected outright."""
    return "manifest_schema: v0\na: &x [1, 2, 3]\nb: *x\n"


def oversize_document_text() -> str:
    """MAN-S002 — a document one comment past the 512 KiB ceiling (B-01)."""
    return "manifest_schema: v0\nbig: " + ("z" * (MAX_DOCUMENT_BYTES + 16))


def too_deep_document_text() -> str:
    """MAN-S003 — nesting past the depth-12 ceiling (B-02)."""
    node: dict[str, Any] = {}
    cursor = node
    for _ in range(MAX_NESTING_DEPTH + 5):
        child: dict[str, Any] = {}
        cursor["k"] = child
        cursor = child
    return yaml.safe_dump(node)


# ---------------------------------------------------------------------------
# Layer-1 schema conformance (MAN-S004).
# ---------------------------------------------------------------------------


def bad_manifest_schema_const() -> dict[str, Any]:
    """MAN-S004 — ``manifest_schema`` is not the ``v0`` const."""
    doc = valid_subset_manifest()
    doc["manifest_schema"] = "v1"
    return doc


# ---------------------------------------------------------------------------
# Referential integrity (MAN-V101…V111).
# ---------------------------------------------------------------------------


def undeclared_actor_entity() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["metadata"]["actor_entity"] = "ghosts"
    return doc


def relationship_source_attr_missing() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["relationships"][0]["source_attribute"] = "nope"
    return doc


def ref_fk_unknown_relationship() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["user_id"]["params"]["relationship"] = "nope"
    return doc


def within_op_on_non_timestamp() -> dict[str, Any]:
    doc = valid_subset_manifest()
    guard = doc["state_machines"]["order_lifecycle"]["states"]["placed"]["transitions"][0]["guard"]
    guard["all"][0]["op"] = "within"  # item_count is numeric, not a timestamp
    return doc


def payload_from_undeclared_created_entity() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"]["order_id"]["from"] = "created.ghost.id"
    return doc


def partition_by_undeclared_created_entity() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["partition_by"] = "created.ghost"
    return doc


def emit_unknown_event_type() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["states"]["started"]["transitions"][0]["emit"] = (
        "nope"
    )
    return doc


def cdc_undeclared_entity() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["cdc"]["entities"]["ghosts"] = {"enabled_default": True}
    return doc


def reserved_df_attribute() -> dict[str, Any]:
    """MAN-V109 (semantic flavor) — a ``_df``-prefixed attribute (SB-1 defence)."""
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["_df_secret"] = {"generator": "text.word"}
    return doc


def created_at_attribute() -> dict[str, Any]:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["created_at"] = {"generator": "time.now"}
    return doc


def seeded_ref_fk_targets_later_declared() -> dict[str, Any]:
    """MAN-V111 — a seeded entity's ``ref.fk`` targets a later-declared seeded entity."""
    doc = valid_subset_manifest()
    doc["relationships"].append(
        {
            "name": "user_last_order",
            "source_entity": "users",
            "source_attribute": "last_order",
            "target_entity": "orders",
            "cardinality": "many_to_one",
        }
    )
    doc["entities"]["users"]["attributes"]["last_order"] = {
        "generator": "ref.fk",
        "params": {"relationship": "user_last_order"},
    }
    return doc


def _clone(doc: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(doc)
