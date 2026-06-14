"""R-DER-2 payload-field fragment derivation (used by V407, V503, and Phase 4).

Maps a manifest ``valueSource`` / ``generatorSpec`` to its JSON Schema fragment per
scenario-plugin-architecture §5.2 R-DER-2. This is the single source of the
type-mapping rule; the registry/Phase-4 schema-derivation agent imports the same
function so the validator's MAN-V407 type checks and the published registry schema
are derived from one implementation (no drift).

Only the fragment **kind** the validator needs (type / enum / pattern) is produced
here; the full closed-document assembly (``required``, ``additionalProperties:
false``) is the registry's R-DER-3 concern.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any

from .generators import GENERATOR_CATALOG

_DECIMAL_PATTERN = r"^-?\d+\.\d{1,4}$"

# Conservative per-fragment serialized-size estimate (bytes) for the B-12/V503
# worst-case payload bound. Strings/objects/arrays use generous upper estimates.
_FRAGMENT_SIZE_ESTIMATE = {
    "string": 1024,
    "integer": 20,
    "number": 24,
    "boolean": 5,
    "object": 4096,
    "array": 8192,
}


def derive_fragment(source: dict[str, Any], key_prefixes: dict[str, str]) -> dict[str, Any]:
    """Derive the R-DER-2 JSON Schema fragment for a payload ``valueSource``.

    ``key_prefixes`` maps relationship-target entity → key_prefix for ``ref.fk``
    patterns (passed in so this stays framework-free and view-agnostic). Returns a
    plain dict fragment; ``nullable: true`` wraps the type as ``["…","null"]``.
    """
    nullable = bool(source.get("nullable", False))
    if "const" in source:
        fragment: dict[str, Any] = {"const": source["const"]}
        return fragment  # const fragments are not nullable-wrapped
    if "generated" in source:
        fragment = _generator_fragment(source["generated"], key_prefixes)
    else:
        # ``from`` paths: the concrete type depends on the referenced attribute,
        # resolved by the registry derivation with full context. For the
        # validator's purposes we treat it as an unconstrained value (object-open),
        # since MAN-V105 already proved the path resolves.
        fragment = {}
    if nullable and "type" in fragment:
        base = fragment["type"]
        fragment["type"] = [base, "null"] if isinstance(base, str) else base
    return fragment


def _generator_fragment(
    spec: dict[str, Any], key_prefixes: dict[str, str]
) -> dict[str, Any]:
    name = spec.get("generator", "")
    catalog = GENERATOR_CATALOG.get(name)
    if catalog is None:
        return {}
    output = catalog.output
    if output == "string":
        return {"type": "string"}
    if output == "integer":
        return {"type": "integer"}
    if output == "number":
        return {"type": "number"}
    if output == "decimal":
        return {"type": "string", "pattern": _DECIMAL_PATTERN}
    if output == "boolean":
        return {"type": "boolean"}
    if output == "datetime":
        return {"type": "string", "format": "date-time"}
    if output == "object":  # address.full
        return _address_object_fragment()
    if output == "entity_key":  # ref.fk
        return _ref_fk_fragment(spec, key_prefixes)
    if output == "enum":  # choice.* — payload enums (R-DER-2)
        return _enum_fragment(spec)
    if output == "scalar":  # ref.attr — concrete type known only with target context
        return {}
    if output == "hook":
        return {}  # output_type resolved from the registered hook (Phase 4)
    return {}


def _address_object_fragment() -> dict[str, Any]:
    props = {
        k: {"type": "string"}
        for k in ("street", "city", "state", "postal_code", "country")
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def _ref_fk_fragment(
    spec: dict[str, Any], key_prefixes: dict[str, str]
) -> dict[str, Any]:
    rel = (spec.get("params", {}) or {}).get("relationship", "")
    prefix = key_prefixes.get(rel, "")
    if prefix:
        return {"type": "string", "pattern": f"^{prefix}_[0-9a-f]{{16}}$"}
    return {"type": "string"}


def _enum_fragment(spec: dict[str, Any]) -> dict[str, Any]:
    options = (spec.get("params", {}) or {}).get("options", [])
    values: list[Any] = []
    for opt in options:
        if isinstance(opt, dict) and "value" in opt:  # choice.weighted
            values.append(opt["value"])
        else:  # choice.uniform scalar
            values.append(opt)
    return {"enum": values}


def fragment_size_estimate(fragment: dict[str, Any]) -> int:
    """A conservative serialized-byte estimate for a derived fragment (B-12/V503)."""
    if "const" in fragment:
        return len(str(fragment["const"])) + 8
    if "enum" in fragment:
        return max((len(str(v)) for v in fragment["enum"]), default=8) + 8
    ftype = fragment.get("type")
    if isinstance(ftype, list):
        ftype = next((t for t in ftype if t != "null"), "string")
    return _FRAGMENT_SIZE_ESTIMATE.get(ftype or "string", 1024)
