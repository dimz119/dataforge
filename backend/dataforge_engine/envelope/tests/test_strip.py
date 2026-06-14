"""``strip_internal`` boundary tests (event-model §5.2, INV-DEL-2, SB-1)."""

from __future__ import annotations

import pytest

from dataforge_engine.envelope import (
    DELIVERED_FIELD_SET,
    RESERVED_PREFIX,
    strip_internal,
)
from dataforge_engine.envelope.strip import StripError

from .fixtures import order_placed_envelope


def test_strip_leaves_exactly_20_keys() -> None:
    delivered = strip_internal(order_placed_envelope())
    assert set(delivered.keys()) == DELIVERED_FIELD_SET
    assert len(delivered) == 20


def test_strip_removes_df_block() -> None:
    delivered = strip_internal(order_placed_envelope())
    assert "_df" not in delivered


def test_no_reserved_prefix_key_survives() -> None:
    """SB-1: any top-level ``_df``-prefixed key is dropped, not just exact ``_df``."""
    env = dict(order_placed_envelope())
    env["_df_extra"] = {"debug": True}
    delivered = strip_internal(env)
    assert not any(k.startswith(RESERVED_PREFIX) for k in delivered)
    assert set(delivered.keys()) == DELIVERED_FIELD_SET


def test_strip_is_idempotent() -> None:
    once = strip_internal(order_placed_envelope())
    twice = strip_internal(dict(once))
    assert once == twice


def test_strip_preserves_delivered_values() -> None:
    internal = order_placed_envelope()
    delivered = strip_internal(internal)
    for key in DELIVERED_FIELD_SET:
        assert delivered[key] == internal[key]  # type: ignore[literal-required]


def test_strip_missing_required_field_raises() -> None:
    env = dict(order_placed_envelope())
    del env["payload"]
    with pytest.raises(StripError):
        strip_internal(env)


def test_strip_unexpected_non_reserved_key_raises() -> None:
    env = dict(order_placed_envelope())
    env["surprise"] = 1
    with pytest.raises(StripError):
        strip_internal(env)
