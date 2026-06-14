"""Canonical serialization tests (event-model §2.4, S-1..S-6)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from dataforge_engine.envelope import (
    DELIVERED_FIELD_ORDER,
    canonical_serialize,
    canonical_serialize_str,
    strip_internal,
)
from dataforge_engine.envelope.serialize import (
    JS_MAX_SAFE_INTEGER,
    SerializationError,
    _encode_value,
)

from .fixtures import order_placed_envelope


def test_top_level_key_order_matches_catalog() -> None:
    """S-2: envelope keys in the §2.1 catalog order, ``_df`` last."""
    text = canonical_serialize_str(order_placed_envelope())
    parsed = json.loads(text)
    keys = list(parsed.keys())
    assert keys[:-1] == list(DELIVERED_FIELD_ORDER)
    assert keys[-1] == "_df"


def test_delivered_serialization_omits_df_and_keeps_order() -> None:
    delivered = strip_internal(order_placed_envelope())
    parsed = json.loads(canonical_serialize_str(delivered))
    assert list(parsed.keys()) == list(DELIVERED_FIELD_ORDER)


def test_no_insignificant_whitespace() -> None:
    """S-2: compact separators — no spaces after ``:`` or ``,``."""
    text = canonical_serialize_str(order_placed_envelope())
    assert ", " not in text
    assert ": " not in text


def test_money_rendered_as_decimal_strings() -> None:
    """S-6: monetary amounts are JSON strings, never floats, with literal digits."""
    text = canonical_serialize_str(order_placed_envelope())
    assert '"total":"64.97"' in text
    assert '"subtotal":"59.97"' in text
    assert '"unit_price":"39.99"' in text
    # The trailing-zero / scale of the Decimal is preserved verbatim.
    assert '"shipping_fee":"4.99"' in text


def test_byte_stable_across_runs() -> None:
    """Same input → byte-identical output, every time (canonical-form contract)."""
    a = canonical_serialize(order_placed_envelope())
    b = canonical_serialize(order_placed_envelope())
    assert a == b
    assert isinstance(a, bytes)


def test_payload_preserves_declared_order() -> None:
    """S-2: payload keys keep declared property order (insertion order)."""
    parsed = json.loads(canonical_serialize_str(order_placed_envelope()))
    assert list(parsed["payload"].keys()) == [
        "order_id", "user_id", "items", "currency",
        "subtotal", "shipping_fee", "total", "shipping_country",
    ]


def test_utf8_no_bom() -> None:
    raw = canonical_serialize(order_placed_envelope())
    assert not raw.startswith(b"\xef\xbb\xbf")
    raw.decode("utf-8")  # must be valid UTF-8


def test_nan_and_infinity_forbidden() -> None:
    with pytest.raises(SerializationError):
        _encode_value(float("nan"))
    with pytest.raises(SerializationError):
        _encode_value(float("inf"))


def test_integer_beyond_js_safe_range_rejected() -> None:
    """S-1: integers must fit < 2**53; an over-range int is a serialization error."""
    with pytest.raises(SerializationError):
        _encode_value(JS_MAX_SAFE_INTEGER + 1)
    # The boundary itself is allowed.
    assert _encode_value(JS_MAX_SAFE_INTEGER) == str(JS_MAX_SAFE_INTEGER)


def test_big_int_as_decimal_string_round_trips() -> None:
    """S-6: a value beyond the JS-safe range travels as a Decimal string."""
    big = Decimal(JS_MAX_SAFE_INTEGER + 1000)
    assert _encode_value(big) == f'"{big}"'


def test_string_escaping() -> None:
    assert _encode_value('a"b\\c') == '"a\\"b\\\\c"'
    assert _encode_value("tab\tnl\n") == '"tab\\tnl\\n"'
    assert _encode_value("\x01") == '"\\u0001"'


def test_bool_before_int() -> None:
    assert _encode_value(True) == "true"
    assert _encode_value(False) == "false"
    assert _encode_value(0) == "0"


def test_missing_field_raises() -> None:
    env = dict(order_placed_envelope())
    del env["payload"]
    with pytest.raises(SerializationError):
        canonical_serialize(env)
