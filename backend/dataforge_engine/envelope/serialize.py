"""Canonical serialization of the envelope (event-model §2.4, rules S-1..S-6).

Canonical form (S-2) is what the ground-truth ledger stores, what golden-seed
fixtures pin, and what byte-identity tests compare. It is *byte-stable*: same
input → same bytes, every run, every machine. The rules realised here:

* S-1 — JSON, UTF-8, no BOM; ``NaN``/``Infinity`` forbidden; integers must fit
  the IEEE-754 double-safe range (< 2**53) for JS clients.
* S-2 — envelope keys in the §2.1 catalog order; ``payload`` keys in the payload
  schema's declared property order (we honour Python ``dict`` insertion order,
  which the builder seeds in declared order); no insignificant whitespace.
* S-6 — monetary / seed / big-int amounts are decimal **strings**, never floats;
  carried in memory as ``Decimal`` and rendered with their literal digits.

This module emits ``bytes`` (the wire/ledger unit) and a ``str`` convenience
wrapper. It does *not* use :func:`json.dumps` for the top level: stdlib JSON
cannot interleave a fixed top-level key order with insertion-ordered nested
dicts *and* render ``Decimal`` as an unquoted-free string without a custom
encoder, so we render deterministically by hand. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from .types import DELIVERED_FIELD_ORDER, INTERNAL_BLOCK_KEY

if TYPE_CHECKING:
    from .types import EnvelopeMapping

# The largest integer JS clients can represent exactly (S-1). Values at or beyond
# this magnitude must travel as decimal strings (S-6), never as JSON numbers.
JS_MAX_SAFE_INTEGER = 2**53 - 1

# Compact JSON separators — no insignificant whitespace (S-2).
_ITEM_SEP = ","
_KEY_SEP = ":"

_ESCAPE_MAP = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class SerializationError(ValueError):
    """Raised when a value cannot be canonically serialized (an S-rule breach)."""


def _encode_string(value: str) -> str:
    out = ['"']
    for ch in value:
        escaped = _ESCAPE_MAP.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_int(value: int) -> str:
    # bool is an int subclass; callers route bools through ``_encode_value`` which
    # checks bool first, so this only ever sees true integers.
    if abs(value) > JS_MAX_SAFE_INTEGER:
        raise SerializationError(
            f"integer {value} exceeds the JS double-safe range (S-1); "
            "carry it as a Decimal string instead (S-6)"
        )
    return str(value)


def _encode_float(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        raise SerializationError("NaN/Infinity are forbidden in canonical JSON (S-1)")
    # ``repr`` gives the shortest round-tripping decimal for a float; integral
    # floats are normalised to ``N.0`` so output stays JSON-number shaped.
    if value == int(value):
        return f"{int(value)}.0"
    return repr(value)


def _encode_decimal(value: Decimal) -> str:
    # Money / seed / big-int (S-6): rendered as a JSON *string* of the literal
    # decimal digits. ``Decimal`` preserves trailing zeros and scale, so "64.97"
    # stays "64.97" and "39.99" stays "39.99" — byte-stable.
    if not value.is_finite():
        raise SerializationError("non-finite Decimal cannot be serialized (S-1)")
    return _encode_string(str(value))


def _encode_value(value: object) -> str:
    # Accepts ``object`` (envelope values read out of a ``Mapping[str, object]``)
    # and narrows at runtime. Order matters: ``bool`` before ``int`` (bool ⊂ int);
    # ``Decimal`` before the numeric branches (Decimal is not int/float).
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _encode_decimal(value)
    if isinstance(value, int):
        return _encode_int(value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, Mapping):
        return _encode_object(value)
    if isinstance(value, list):
        return "[" + _ITEM_SEP.join(_encode_value(item) for item in value) + "]"
    raise SerializationError(f"cannot canonically serialize value of type {type(value)!r}")


def _encode_object(obj: Mapping[str, object]) -> str:
    # Nested objects preserve *insertion order* (S-2: payload keys in declared
    # property order; the builder seeds payloads in declared order, and CDC
    # sub-shapes are inserted in §4.2 order). Non-string keys are rejected (a
    # canonical JSON object has string keys only, S-1).
    parts: list[str] = []
    for key, val in obj.items():
        if not isinstance(key, str):
            raise SerializationError(f"object key must be a string, got {type(key)!r} (S-1)")
        parts.append(_encode_string(key) + _KEY_SEP + _encode_value(val))
    return "{" + _ITEM_SEP.join(parts) + "}"


def canonical_serialize(envelope: EnvelopeMapping) -> bytes:
    """Render an internal *or* delivered envelope to canonical bytes (S-2).

    Top-level keys are emitted in the §2.1 catalog order; if an internal ``_df``
    block is present it is emitted last (after field 20), matching the §2.1 field
    ordering where ``_df`` is field 21. ``payload`` and every other nested object
    keep insertion order. Output is UTF-8 with no BOM and no insignificant
    whitespace (S-1/S-2).
    """
    parts: list[str] = []
    for key in DELIVERED_FIELD_ORDER:
        if key not in envelope:
            raise SerializationError(
                f"envelope is missing required field {key!r} — "
                "all 20 keys must be present in envelope 1.0 (§2.1)"
            )
        parts.append(_encode_string(key) + _KEY_SEP + _encode_value(envelope[key]))
    if INTERNAL_BLOCK_KEY in envelope:
        parts.append(
            _encode_string(INTERNAL_BLOCK_KEY)
            + _KEY_SEP
            + _encode_value(envelope[INTERNAL_BLOCK_KEY])
        )
    text = "{" + _ITEM_SEP.join(parts) + "}"
    return text.encode("utf-8")


def canonical_serialize_str(envelope: EnvelopeMapping) -> str:
    """:func:`canonical_serialize` decoded to ``str`` (convenience for tests/logs)."""
    return canonical_serialize(envelope).decode("utf-8")
