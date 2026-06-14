"""``BACKWARD_ADDITIVE`` compatibility checker (schema-registry §6, INV-REG-3).

The §6.4 worked rejections are the normative corpus: each candidate change against
``order_placed`` v1 has a fixed verdict + code. Pure logic — no DB.
"""

from __future__ import annotations

import copy
from typing import Any

from registry.infra.compat import check_backward_additive


def _v1() -> dict[str, Any]:
    """A closed v1 schema in the §9.2 order_placed shape (subset)."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://docs.dataforge.dev/schemas/ecommerce.order_placed/versions/1.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["order_id", "currency", "shipping_fee", "items"],
        "properties": {
            "order_id": {"type": "string", "pattern": "^ord_[0-9a-f]{16}$"},
            "currency": {"const": "USD"},
            "shipping_fee": {"type": "string", "pattern": r"^-?\d+\.\d{1,4}$"},
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["quantity"],
                "properties": {"quantity": {"type": "integer"}},
            },
        },
    }


def _codes(latest: dict[str, Any], candidate: dict[str, Any]) -> set[str]:
    return {e.code for e in check_backward_additive(latest, candidate)}


def test_identical_schema_is_compatible() -> None:
    assert check_backward_additive(_v1(), copy.deepcopy(_v1())) == []


def test_annotation_only_change_is_no_change() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["description"] = "a different description"
    candidate["properties"]["order_id"]["x-df-binding"] = "actor.order_id"
    assert check_backward_additive(_v1(), candidate) == []


def test_add_optional_field_is_accepted() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["shipping_state"] = {"type": "string"}
    # NOT added to required → BACKWARD_ADDITIVE accepts it.
    assert check_backward_additive(_v1(), candidate) == []


def test_add_field_and_require_it_is_rejected_c003() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["shipping_state"] = {"type": "string"}
    candidate["required"].append("shipping_state")
    assert "REG-C003" in _codes(_v1(), candidate)


def test_drop_field_is_rejected_c001() -> None:
    candidate = copy.deepcopy(_v1())
    del candidate["properties"]["shipping_fee"]
    candidate["required"].remove("shipping_fee")
    assert "REG-C001" in _codes(_v1(), candidate)


def test_retype_nested_field_is_rejected_c002() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["items"]["properties"]["quantity"] = {"type": "string"}
    assert "REG-C002" in _codes(_v1(), candidate)


def test_enum_widening_is_rejected_c002() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["currency"] = {"enum": ["USD", "EUR"]}
    assert "REG-C002" in _codes(_v1(), candidate)


def test_open_document_is_rejected_c004() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["additionalProperties"] = True
    assert "REG-C004" in _codes(_v1(), candidate)


def test_reserved_field_name_is_rejected_c009() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["_df_grade"] = {"type": "string"}
    assert "REG-C009" in _codes(_v1(), candidate)


def test_non_object_top_level_is_rejected_c005() -> None:
    candidate = {"type": "array", "items": {"type": "object"}}
    assert "REG-C005" in _codes(_v1(), candidate)


def test_unsupported_construct_is_rejected_c006() -> None:
    candidate = copy.deepcopy(_v1())
    candidate["properties"]["weird"] = {"oneOf": [{"type": "string"}]}
    assert "REG-C006" in _codes(_v1(), candidate)


def test_errors_carry_json_pointer_paths() -> None:
    candidate = copy.deepcopy(_v1())
    del candidate["properties"]["shipping_fee"]
    candidate["required"].remove("shipping_fee")
    errors = check_backward_additive(_v1(), candidate)
    removal = next(e for e in errors if e.code == "REG-C001")
    assert removal.path == "/properties/shipping_fee"
