"""Deterministic UUIDv7 ``event_id`` tests (event-model §2.2.1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dataforge_engine.envelope import build_uuidv7, event_id_for
from dataforge_engine.envelope.event_id import EventIdError
from dataforge_engine.envelope.timestamps import occurred_at_ms

from .fixtures import SeededRandomBits

_OCC = datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=UTC)


def test_same_seed_and_occurred_at_yield_same_event_id() -> None:
    """Determinism: same (seed, occurred_at) → identical event_id (INV-GEN-3)."""
    a = event_id_for(_OCC, SeededRandomBits(4242))
    b = event_id_for(_OCC, SeededRandomBits(4242))
    assert a == b


def test_different_seed_yields_different_event_id() -> None:
    a = event_id_for(_OCC, SeededRandomBits(4242))
    b = event_id_for(_OCC, SeededRandomBits(9999))
    assert a != b


def test_version_and_variant_nibbles() -> None:
    """RFC 9562 layout: version nibble = 7, variant top bits = 0b10."""
    uid = build_uuidv7(timestamp_ms=occurred_at_ms(_OCC), random_74=0)
    assert uid.version == 7
    # The variant is encoded in the high bits of byte 8 (clock_seq_hi).
    assert (uid.bytes[8] & 0xC0) == 0x80


def test_timestamp_bits_encode_occurred_at_ms() -> None:
    """The top 48 bits equal occurred_at milliseconds (simulated time)."""
    ms = occurred_at_ms(_OCC)
    uid = build_uuidv7(timestamp_ms=ms, random_74=12345)
    embedded = uid.int >> 80
    assert embedded == ms


def test_random_bits_placement() -> None:
    """All 74 random bits land in rand_a (12) + rand_b (62), nowhere else."""
    uid = build_uuidv7(timestamp_ms=0, random_74=(1 << 74) - 1)
    # Strip the timestamp (top 48), version (4), variant (2): the rest is random.
    rand_a = (uid.int >> 64) & 0x0FFF
    rand_b = uid.int & ((1 << 62) - 1)
    assert rand_a == 0x0FFF
    assert rand_b == (1 << 62) - 1


def test_lowercase_canonical_string() -> None:
    s = event_id_for(_OCC, SeededRandomBits(1))
    assert s == s.lower()
    assert len(s) == 36
    assert s.count("-") == 4


def test_out_of_range_inputs_rejected() -> None:
    with pytest.raises(EventIdError):
        build_uuidv7(timestamp_ms=-1, random_74=0)
    with pytest.raises(EventIdError):
        build_uuidv7(timestamp_ms=1 << 48, random_74=0)
    with pytest.raises(EventIdError):
        build_uuidv7(timestamp_ms=0, random_74=1 << 74)


def test_k_sortable_by_event_time() -> None:
    """Lexicographic id order ≈ event-time order (later occurred_at → larger id)."""
    early = datetime(2026, 6, 10, 14, 0, 0, 0, tzinfo=UTC)
    late = datetime(2026, 6, 10, 15, 0, 0, 0, tzinfo=UTC)
    id_early = event_id_for(early, SeededRandomBits(1))
    id_late = event_id_for(late, SeededRandomBits(1))
    assert id_early < id_late
