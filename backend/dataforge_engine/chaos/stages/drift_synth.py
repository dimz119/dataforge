"""Type-directed drift value synthesis (chaos-engine §5.5 table, DR-2).

Drift has only the registry schema (not the next manifest's generators), so it
synthesizes each added field's value from its JSON-Schema FRAGMENT, deterministic
from the ``chaos`` sub-seed (INV-CHA-2). The closed vocabulary (SD-3) is what
makes the synthesis total — every fragment maps to exactly one branch.

Pure Python (BE-ENG-1): no Django, no DB; the fragment is data the registry_view
port already resolved (DR-1).
"""

from __future__ import annotations

import re
from typing import TypedDict, cast

from dataforge_engine.envelope.types import JSONValue

from ..prf import draw_u, draw_u64, weighted_choice

# A fixed 64-word pool for plain-string synthesis (§5.5: "seeded token from a
# fixed 64-word pool"). Frozen so values are byte-stable across replays.
_WORD_POOL: tuple[str, ...] = (
    "harbor", "amber", "cobalt", "ember", "fjord", "glade", "harvest", "indigo",
    "juniper", "kestrel", "lumen", "marble", "nimbus", "onyx", "pewter", "quartz",
    "ravine", "saffron", "thicket", "umber", "verdant", "willow", "xenon", "yarrow",
    "zephyr", "alabaster", "basalt", "cinder", "dune", "estuary", "frost", "granite",
    "hazel", "iris", "jasper", "kelp", "lichen", "meadow", "nectar", "opal",
    "plume", "quill", "reef", "slate", "talon", "umbra", "vellum", "wisp",
    "azure", "bramble", "crest", "delta", "elm", "fern", "grove", "heath",
    "ivory", "jade", "knoll", "loam", "mica", "nova", "ochre", "petal",
)

# entity-key pattern ``^{prefix}_[0-9a-f]{16}$`` (§5.5 row).
_ENTITY_KEY_RE = re.compile(r"^\^([a-z_]+)_\[0-9a-f\]\{16\}\$$")
# decimal-string pattern detection: any pattern mentioning a backslash-d run + dot.
_DECIMAL_HINT = re.compile(r"\\d")


class DriftField(TypedDict):
    """One menu entry (DR-1): the field ``path`` plus its next-version ``fragment``."""

    path: str
    fragment: dict[str, object]


def synthesize_value(
    fragment: dict[str, object],
    subseed: bytes,
    event_id: str,
    label: str,
    *,
    occurred_at: str,
    depth: int = 0,
) -> JSONValue:
    """Synthesize a schema-valid value for ``fragment`` (§5.5 table; total over SD-3)."""
    if "const" in fragment:
        return cast(JSONValue, fragment["const"])
    types = _normalize_type(fragment.get("type"))
    if "null" in types and len(types) > 1:  # nullable: non-null branch (§5.5)
        return _synth_typed(
            {**fragment, "type": [t for t in types if t != "null"]},
            subseed, event_id, label, occurred_at, depth,
        )
    enum = fragment.get("enum")
    if isinstance(enum, list) and enum:
        u = draw_u(subseed, "schema_drift", event_id, f"{label}:enum")
        return cast(JSONValue, enum[weighted_choice(u, [1.0] * len(enum))])
    return _synth_typed(fragment, subseed, event_id, label, occurred_at, depth)


def _normalize_type(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def _synth_typed(
    fragment: dict[str, object],
    subseed: bytes,
    event_id: str,
    label: str,
    occurred_at: str,
    depth: int,
) -> JSONValue:
    types = _normalize_type(fragment.get("type"))
    primary = types[0] if types else "string"
    if primary == "boolean":
        return draw_u64(subseed, "schema_drift", event_id, f"{label}:bool") % 2 == 0
    if primary == "integer":
        return _synth_int(fragment, subseed, event_id, label)
    if primary == "number":
        return float(_synth_int(fragment, subseed, event_id, label))
    if primary == "array":
        if depth >= 4:
            return []
        items = fragment.get("items")
        item_frag = items if isinstance(items, dict) else {"type": "string"}
        return [synthesize_value(
            item_frag, subseed, event_id, f"{label}[0]",
            occurred_at=occurred_at, depth=depth + 1,
        )]
    if primary == "object":
        return _synth_object(fragment, subseed, event_id, label, occurred_at, depth)
    return _synth_string(fragment, subseed, event_id, label, occurred_at)


def _synth_int(
    fragment: dict[str, object], subseed: bytes, event_id: str, label: str
) -> int:
    lo = fragment.get("minimum")
    hi = fragment.get("maximum")
    low = int(lo) if isinstance(lo, (int, float)) else 0
    high = int(hi) if isinstance(hi, (int, float)) else 1000
    if high < low:
        high = low
    span = high - low + 1
    return low + int(draw_u64(subseed, "schema_drift", event_id, f"{label}:int") % span)


def _synth_string(
    fragment: dict[str, object], subseed: bytes, event_id: str, label: str, occurred_at: str
) -> JSONValue:
    if fragment.get("format") == "date-time":  # the target event's occurred_at (§5.5)
        return occurred_at
    pattern = fragment.get("pattern")
    if isinstance(pattern, str):
        ek = _ENTITY_KEY_RE.match(pattern)
        if ek:  # entity-key token: synthetic, schema-valid, referentially meaningless
            tok = f"{draw_u64(subseed, 'schema_drift', event_id, f'{label}:ek'):016x}"
            return f"{ek.group(1)}_{tok}"
        if _DECIMAL_HINT.search(pattern):  # decimal-string, 2-digit scale (§5.5)
            cents = draw_u64(subseed, "schema_drift", event_id, f"{label}:dec") % 100000
            return f"{cents // 100}.{cents % 100:02d}"
    u = draw_u(subseed, "schema_drift", event_id, f"{label}:word")
    return _WORD_POOL[weighted_choice(u, [1.0] * len(_WORD_POOL))]


def _synth_object(
    fragment: dict[str, object],
    subseed: bytes,
    event_id: str,
    label: str,
    occurred_at: str,
    depth: int,
) -> JSONValue:
    if depth >= 4:
        return {}
    props = fragment.get("properties")
    required = fragment.get("required")
    req = set(required) if isinstance(required, list) else set()
    out: dict[str, JSONValue] = {}
    if isinstance(props, dict):
        for name, sub in props.items():
            if req and name not in req:
                continue
            sub_frag = sub if isinstance(sub, dict) else {"type": "string"}
            out[name] = synthesize_value(
                sub_frag, subseed, event_id, f"{label}.{name}",
                occurred_at=occurred_at, depth=depth + 1,
            )
    return out
