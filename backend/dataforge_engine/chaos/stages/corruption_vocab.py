"""The closed corruption vocabulary (chaos-engine §5.3).

The ONLY permitted mutations. Each kind has a fixed mutation and the set of value
types it is valid for. Every kind produces a payload that violates the pinned
``schema_ref`` (derived schemas are closed and exactly typed) — corruption is
always machine-detectable, which is what makes E6's DLQ contents gradable.

In the Phase-9-modes-1-4 stage the target's type is inferred from the runtime
JSON value (string / int / number / bool / date-time-string); a registry-view
type resolution refines the enum/decimal-string/date-time distinctions in the
drift-aware stage work. The inference is deterministic, so byte-identity holds.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

from dataforge_engine.envelope.types import JSONValue

# A coarse value-type tag inferred from the runtime leaf (sufficient for kind
# validity in modes 1-4). ``decimal_string`` is a numeric string like "64.97";
# ``datetime_string`` is an RFC-3339 instant; ``plain_string`` is everything else.
ValueType = str

_DECIMAL_RE: Final = re.compile(r"^-?\d+(\.\d+)?$")
_DATETIME_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def infer_value_type(value: JSONValue) -> ValueType:
    """Infer the coarse value type used to pick a valid corruption kind (§5.3)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        if _DATETIME_RE.match(value):
            return "datetime_string"
        if _DECIMAL_RE.match(value):
            return "decimal_string"
        return "plain_string"
    return "plain_string"


# kind -> (valid value types, mutation function). Closed; the ONLY permitted set.
_VOCAB: Final[dict[str, tuple[frozenset[str], Callable[[JSONValue], JSONValue]]]] = {
    "alpha_string": (frozenset({"decimal_string", "integer", "number"}), lambda _v: "abc"),
    "locale_comma": (frozenset({"decimal_string"}), lambda v: str(v).replace(".", ",")),
    "empty_string": (frozenset({"plain_string", "decimal_string"}), lambda _v: ""),
    "truncate": (frozenset({"plain_string"}), lambda v: str(v)[:1] if len(str(v)) > 1 else str(v)),
    "mojibake": (frozenset({"plain_string"}), lambda _v: "Ã©Ã¶Ã±"),
    "wrong_type_number": (frozenset({"plain_string"}), lambda _v: 123),
    "string_wrap": (frozenset({"integer", "number", "boolean"}), lambda v: f'"{v}"'),
    "negative_flip": (frozenset({"integer", "number"}), lambda v: -v if isinstance(v, (int, float)) else v),  # noqa: E501
    "int_overflow": (frozenset({"integer"}), lambda _v: 9007199254740993),
    "epoch_millis": (frozenset({"datetime_string"}), lambda _v: 1781187785123),
    "invalid_timestamp": (frozenset({"datetime_string"}), lambda _v: "2026-13-45T99:99:99Z"),
    "naive_local": (frozenset({"datetime_string"}), lambda v: str(v).rstrip("Z")),
    "bool_string": (frozenset({"boolean"}), lambda _v: "yes"),
}

# The full closed kind vocabulary (CH-V08 membership check on the Django side).
CORRUPTION_KINDS: Final[tuple[str, ...]] = tuple(_VOCAB.keys())


def valid_kinds_for(value_type: ValueType) -> list[str]:
    """The corruption kinds valid for a target's inferred ``value_type`` (§5.3).

    Returned in the fixed ``_VOCAB`` declaration order so the seeded kind choice is
    deterministic across runs.
    """
    return [kind for kind, (types, _fn) in _VOCAB.items() if value_type in types]


def apply_kind(kind: str, value: JSONValue) -> JSONValue:
    """Apply one corruption ``kind`` to ``value`` (the closed mutation table)."""
    return _VOCAB[kind][1](value)
