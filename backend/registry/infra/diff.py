"""Computed added-fields diff between two schema versions (schema-registry §5.3/§7).

The added-fields diff is **computed, never stored** (§3.2 is exhaustive — there is
no diff column): ``added(N) = properties(N) \\ properties(N-1)``, recursively
including fields added *inside* an existing nested object or array-item object.
Under ``BACKWARD_ADDITIVE`` (the only MVP mode) this diff is complete by
construction — nothing is ever removed or retyped (INV-REG-3) — so ``removed`` and
``changed`` are always empty; they exist in :func:`diff_versions`' return shape so
the #66 contract (api-spec §4.12) survives a future compatibility-mode addition
without a wire break (V-2).

Three consumers share this pure helper (§5.3): the diff API (§7 #66), the upgrade
resolver extension (§10.4), and the drift field menu (§11). Each ``added`` entry
is ``{path, type, required}`` — ``path`` a JSON Pointer into the *to* document
(``/properties/shipping_state``, ``/properties/items/items/properties/x`` for a
nested addition), ``type`` mirroring the added fragment's ``type`` keyword (the
array form for nullable fields; an ``enum``/``const`` fragment reports its
underlying primitive type — the full fragment lives in the version document), and
``required`` always ``False`` (REQ-RULE: added fields are optional).

Pure logic — no Django imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Any


@dataclass(frozen=True)
class AddedField:
    """One added property of the candidate relative to the prior version (#66)."""

    path: str  # JSON Pointer into the *to* document
    type: str  # the fragment's primitive type (array form for nullable)
    required: bool  # always False under REQ-RULE (kept explicit for the wire shape)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "type": self.type, "required": self.required}


@dataclass(frozen=True)
class SchemaDiff:
    """The §7 #66 computed diff: additions only under BACKWARD_ADDITIVE (INV-REG-3)."""

    added_fields: list[AddedField]
    removed_fields: list[AddedField]  # always empty (INV-REG-3)
    changed_fields: list[AddedField]  # always empty (INV-REG-3)


def diff_versions(from_document: dict[str, Any], to_document: dict[str, Any]) -> SchemaDiff:
    """Compute the added-fields diff registering ``to`` after ``from`` (§5.3).

    ``added`` walks both closed object documents in lock-step: a property of ``to``
    absent from ``from`` is an addition at this level; a property present in both
    that is itself a closed object (or an array of closed objects) recurses, so a
    field introduced *inside* an existing nested structure is reported with its full
    JSON Pointer. ``removed``/``changed`` are empty by construction here (the
    BACKWARD_ADDITIVE gate has already rejected any removal/retype before a version
    is registered) but are returned for the future-proof #66 shape (V-2).
    """
    added: list[AddedField] = []
    _collect_added(from_document, to_document, "", added)
    return SchemaDiff(added_fields=added, removed_fields=[], changed_fields=[])


def diff_range(documents: list[dict[str, Any]]) -> SchemaDiff:
    """Aggregate the per-step added-fields diffs across an ordered version chain (§7).

    ``documents`` is the contiguous list of version documents from ``from`` to
    ``to`` (ascending). A multi-step range (e.g. 1→3) is the concatenation of each
    adjacent per-version diff, so additions appear in **version-introduction order**
    deterministically — independent of any single document's stored key order (a
    jsonb round-trip does not preserve object key order). Under BACKWARD_ADDITIVE
    each step only adds, so the concatenation has no duplicates and ``removed`` /
    ``changed`` stay empty (INV-REG-3).
    """
    aggregated: list[AddedField] = []
    for prev, curr in pairwise(documents):
        aggregated.extend(diff_versions(prev, curr).added_fields)
    return SchemaDiff(added_fields=aggregated, removed_fields=[], changed_fields=[])


def _collect_added(
    left: dict[str, Any], right: dict[str, Any], pointer: str, added: list[AddedField]
) -> None:
    """Recursively collect properties of ``right`` absent from ``left``."""
    left_props: dict[str, Any] = left.get("properties", {}) or {}
    right_props: dict[str, Any] = right.get("properties", {}) or {}
    for name, fragment in right_props.items():
        prop_ptr = f"{pointer}/properties/{name}"
        if name not in left_props:
            added.append(
                AddedField(path=prop_ptr, type=_fragment_type(fragment), required=False)
            )
            continue
        # A common property: recurse into closed sub-objects / array-item objects so
        # a field added inside an existing nested structure is still an addition.
        l_frag = left_props[name]
        if _is_closed_object(l_frag) and _is_closed_object(fragment):
            _collect_added(l_frag, fragment, prop_ptr, added)
        elif _is_object_array(l_frag) and _is_object_array(fragment):
            _collect_added(l_frag["items"], fragment["items"], f"{prop_ptr}/items", added)


def _fragment_type(fragment: Any) -> str:
    """The reported ``type`` for an added fragment (§7: array form for nullable).

    A plain scalar/array/object fragment reports its ``type`` keyword (a ``[t,
    "null"]`` nullable list is joined as ``"t|null"``); an ``enum``/``const``
    fragment reports the underlying primitive type of its member(s) — the full
    fragment is always available in the version document for richer rendering.
    """
    if not isinstance(fragment, dict):
        return "unknown"
    declared = fragment.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        return "|".join(str(t) for t in declared)
    if "enum" in fragment:
        return _scalar_type_of(list(fragment["enum"]))
    if "const" in fragment:
        return _scalar_type_of([fragment["const"]])
    return "unknown"


def _scalar_type_of(values: list[Any]) -> str:
    if values and all(isinstance(v, bool) for v in values):
        return "boolean"
    if values and all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "integer"
    if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "number"
    return "string"


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
