"""``BACKWARD_ADDITIVE`` compatibility enforcement (schema-registry §6, INV-REG-3).

Registration of a candidate schema ``C`` against the latest ``L`` is accepted iff
**all** §6.2 checks pass; each failure is one :class:`CompatError`
``{code, path, message}`` (path = JSON Pointer into ``C``). Checks run in order
but all violations are collected and reported together.

Both schemas are reduced to *comparison form* (``registry.infra.canonical``,
annotations stripped) before checking — a description- or binding-only change is
no change. Nested objects and array-item objects are compared recursively under
the same rules: a field added inside an existing nested object is an addition
(optional); any other nested difference is REG-C002.

On Flow 1 (derivation at manifest publish), C004-C006/C009/C010 are pre-empted by
manifest validation (a valid manifest cannot derive an ill-formed document), so
the live Flow-1 failures are C001/C002/C003 — a minor version that removes or
retypes a payload field. The full check set is implemented here so Flow 2 (Phase
10) and the GUARD/registry tests exercise every code. Pure logic — no Django.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from registry.infra.canonical import comparison_form

# SD-3 fragment vocabulary: the closed set of fragment shapes a property may use
# (the keys that legitimately appear at a fragment's top level).
_ALLOWED_FRAGMENT_KEYS: frozenset[str] = frozenset(
    {"type", "format", "pattern", "enum", "const", "properties", "required",
     "additionalProperties", "items"}
)
_PROP_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_RESERVED_PREFIX = "_df"


@dataclass(frozen=True)
class CompatError:
    """One §6.2 compatibility violation (the §6.3 ``{code, path, message}`` shape)."""

    code: str
    path: str  # JSON Pointer into the candidate document
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def check_backward_additive(latest: dict[str, Any], candidate: dict[str, Any]) -> list[CompatError]:
    """Return every §6.2 violation registering ``candidate`` against ``latest`` (empty ⇒ ok)."""
    left = comparison_form(latest)
    right = comparison_form(candidate)
    errors: list[CompatError] = []
    _check_shape(right, "", errors)
    if errors:
        return errors  # C004/C005/C006/C009/C010 — candidate is not a valid closed doc
    _compare_object(left, right, "", errors)
    return errors


def _check_shape(node: dict[str, Any], pointer: str, errors: list[CompatError]) -> None:
    """SD-1/SD-3 shape checks on the candidate (REG-C004/C005/C006/C009)."""
    if node.get("type") != "object" or "properties" not in node or "required" not in node:
        errors.append(
            CompatError("REG-C005", pointer or "/", "schema must be a closed object document")
        )
        return
    if node.get("additionalProperties") is not False:
        errors.append(
            CompatError(
                "REG-C004",
                f"{pointer}/additionalProperties",
                "document must remain closed (additionalProperties: false)",
            )
        )
    for name, fragment in (node.get("properties") or {}).items():
        prop_ptr = f"{pointer}/properties/{name}"
        _check_property_name(name, prop_ptr, errors)
        _check_fragment(fragment, name, prop_ptr, errors)


def _check_property_name(name: str, pointer: str, errors: list[CompatError]) -> None:
    if name.startswith(_RESERVED_PREFIX) or not re.match(_PROP_NAME_PATTERN, name):
        errors.append(
            CompatError("REG-C009", pointer, f"field name '{name}' is invalid or reserved")
        )


def _check_fragment(
    fragment: Any, name: str, pointer: str, errors: list[CompatError]
) -> None:
    if not isinstance(fragment, dict):
        errors.append(
            CompatError("REG-C006", pointer, f"field '{name}' uses an unsupported schema construct")
        )
        return
    extra = set(fragment.keys()) - _ALLOWED_FRAGMENT_KEYS
    if extra:
        errors.append(
            CompatError("REG-C006", pointer, f"field '{name}' uses an unsupported schema construct")
        )
    # Recurse into closed sub-objects and array-item objects.
    if fragment.get("type") == "object" and "properties" in fragment:
        _check_shape(fragment, pointer, errors)
    items = fragment.get("items")
    if isinstance(items, dict) and items.get("type") == "object":
        _check_shape(items, f"{pointer}/items", errors)


def _compare_object(
    left: dict[str, Any], right: dict[str, Any], pointer: str, errors: list[CompatError]
) -> None:
    """Recursively apply REG-C001/C002/C003 between two closed objects."""
    left_props: dict[str, Any] = left.get("properties", {}) or {}
    right_props: dict[str, Any] = right.get("properties", {}) or {}

    # REG-C001: a property of L absent from C (removal).
    for name in left_props:
        if name not in right_props:
            errors.append(
                CompatError(
                    "REG-C001",
                    f"{pointer}/properties/{name}",
                    f"field '{name}' removed; BACKWARD_ADDITIVE permits additions only",
                )
            )

    # REG-C002: a common property changed (recurse into nested objects/arrays).
    for name in left_props:
        if name not in right_props:
            continue
        l_frag = left_props[name]
        r_frag = right_props[name]
        if _is_closed_object(l_frag) and _is_closed_object(r_frag):
            _compare_object(l_frag, r_frag, f"{pointer}/properties/{name}", errors)
        elif _is_object_array(l_frag) and _is_object_array(r_frag):
            _compare_object(
                l_frag["items"], r_frag["items"], f"{pointer}/properties/{name}/items", errors
            )
        elif l_frag != r_frag:
            errors.append(
                CompatError(
                    "REG-C002",
                    f"{pointer}/properties/{name}",
                    f"field '{name}' changed from {_summary(l_frag)} to {_summary(r_frag)}; "
                    "existing fields are frozen",
                )
            )

    # REG-C003: required(C) != required(L) as sets.
    if set(left.get("required", []) or []) != set(right.get("required", []) or []):
        errors.append(
            CompatError(
                "REG-C003",
                f"{pointer}/required",
                "required set changed; new fields must be optional and existing required "
                "fields stay required (REQ-RULE)",
            )
        )


def _is_closed_object(fragment: Any) -> bool:
    return (
        isinstance(fragment, dict)
        and fragment.get("type") == "object"
        and "properties" in fragment
    )


def _is_object_array(fragment: Any) -> bool:
    return (
        isinstance(fragment, dict)
        and fragment.get("type") == "array"
        and _is_closed_object(fragment.get("items"))
    )


def _summary(fragment: Any) -> str:
    """A short, content-bounded fragment summary for the §6.2 message (AI-2: no echo)."""
    if isinstance(fragment, dict):
        if "type" in fragment:
            return str(fragment["type"])
        if "enum" in fragment:
            return "enum"
        if "const" in fragment:
            return "const"
    return "value"
