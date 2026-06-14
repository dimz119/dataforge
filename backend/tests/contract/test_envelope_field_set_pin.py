"""CON — permanent envelope field-set pin (event-model §2.1, §8 EV-6).

The exact 20-key delivered field set is the published compatibility contract.
This test is PERMANENT and unskippable: an unannounced field appearing in (or a
field vanishing from) the delivered envelope fails the build (EV-6 — "the CI
contract test pins the exact field set per envelope_version"). An addition is
allowed only via the §8 process, which includes updating the frozen list below.
"""

from __future__ import annotations

from dataforge_engine.envelope import (
    DELIVERED_FIELD_ORDER,
    DELIVERED_FIELD_SET,
    strip_internal,
)
from dataforge_engine.envelope.tests.fixtures import order_placed_envelope

# The frozen 20 keys of envelope 1.0, transcribed from event-model §2.1 fields
# 1..20. Editing this set requires the full §8 evolution process (a superseding
# ADR + doc update + schema regeneration + golden-fixture update).
_PINNED_20: frozenset[str] = frozenset(
    {
        "envelope_version", "event_id", "workspace_id", "stream_id", "shard_id",
        "scenario_slug", "manifest_version", "event_type", "schema_ref", "sequence_no",
        "partition_key", "occurred_at", "emitted_at", "actor_id", "session_id",
        "entity_refs", "correlation_id", "causation_id", "op", "payload",
    }
)


def test_pinned_set_has_exactly_20_keys() -> None:
    assert len(_PINNED_20) == 20


def test_library_constant_matches_pin() -> None:
    assert DELIVERED_FIELD_SET == _PINNED_20
    assert frozenset(DELIVERED_FIELD_ORDER) == _PINNED_20
    assert len(DELIVERED_FIELD_ORDER) == 20


def test_delivered_business_envelope_has_exactly_the_pinned_set() -> None:
    delivered = strip_internal(order_placed_envelope())
    assert frozenset(delivered.keys()) == _PINNED_20
    assert len(delivered) == 20


def test_no_field_starts_with_reserved_prefix() -> None:
    """SB-1: no delivered key may begin with ``_df`` at the top level."""
    delivered = strip_internal(order_placed_envelope())
    assert not any(key.startswith("_df") for key in delivered)
