"""Shared test fixtures for the envelope library (pure Python — no Django).

Provides a deterministic :class:`RandomBitsSource` (a seeded PRNG, so tests
exercise the §2.2.1 contract without the engine's real seed machinery), and a
builder for the canonical ``order_placed`` envelope from event-model §7.1 that
the round-trip / determinism / schema tests share.

The PRNG here mimics what the caller will own in Phase 4 (the ``values`` sub-seed
stream): a Mersenne Twister keyed by a fixed seed yields a reproducible draw
order. The envelope library itself never touches ``random`` — the seed stream is
strictly the caller's, which is exactly what we model here.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal

from dataforge_engine.envelope import (
    InternalEnvelope,
    build_internal_envelope,
    event_id_for,
    make_canonical_df,
    make_schema_ref,
)
from dataforge_engine.envelope.types import EntityRef, JSONValue

# Stream constants from event-model §7 (one shared illustrative stream).
WORKSPACE_ID = "0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60"
STREAM_ID = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"
SCENARIO_SLUG = "ecommerce"
MANIFEST_VERSION = "1.0.0"

_RANDOM_74_BITS = 74
_RANDOM_74_CEIL = 1 << _RANDOM_74_BITS


class SeededRandomBits:
    """A reproducible :class:`~dataforge_engine.envelope.RandomBitsSource`.

    One :meth:`next_random_74` call returns the next 74-bit draw from a Mersenne
    Twister keyed by ``seed`` — same seed, same draw order, same values, every
    run (the determinism contract the real caller honours, ADR-0008).
    """

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def next_random_74(self) -> int:
        return self._rng.randrange(_RANDOM_74_CEIL)


def order_placed_envelope(
    *,
    seed: int = 4242,
    occurred_at: datetime | None = None,
) -> InternalEnvelope:
    """Build the canonical ``order_placed`` internal envelope (event-model §7.1).

    Monetary fields are carried as :class:`~decimal.Decimal` (rendered as strings
    by the serializer, S-6). The ``event_id`` is derived deterministically from
    ``occurred_at`` ms + the seeded draw, so the same ``(seed, occurred_at)``
    yields the same id (INV-GEN-3).
    """
    occ = occurred_at or datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=UTC)
    emitted = datetime(2026, 6, 10, 14, 23, 5, 287113, tzinfo=UTC)
    rng = SeededRandomBits(seed)
    event_id = event_id_for(occ, rng)

    entity_refs: list[EntityRef] = [
        {"entity_type": "users", "entity_key": "usr_a3f81c2e9b4d"},
        {"entity_type": "orders", "entity_key": "ord_5f2e7d1a8c3b"},
        {"entity_type": "products", "entity_key": "prd_9c4b2a6e1f8d"},
        {"entity_type": "products", "entity_key": "prd_3e7a5d9b2c6f"},
    ]
    payload: dict[str, JSONValue] = {
        "order_id": "ord_5f2e7d1a8c3b",
        "user_id": "usr_a3f81c2e9b4d",
        "items": [
            {"product_id": "prd_9c4b2a6e1f8d", "quantity": 1, "unit_price": Decimal("39.99")},
            {"product_id": "prd_3e7a5d9b2c6f", "quantity": 2, "unit_price": Decimal("9.99")},
        ],
        "currency": "USD",
        "subtotal": Decimal("59.97"),
        "shipping_fee": Decimal("4.99"),
        "total": Decimal("64.97"),
        "shipping_country": "US",
    }
    return build_internal_envelope(
        event_id=event_id,
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        shard_id=0,
        scenario_slug=SCENARIO_SLUG,
        manifest_version=MANIFEST_VERSION,
        event_type="order_placed",
        schema_ref=make_schema_ref(SCENARIO_SLUG, "order_placed", 1),
        sequence_no=48213,
        partition_entity_type="users",
        partition_entity_key="usr_a3f81c2e9b4d",
        occurred_at=occ,
        emitted_at=emitted,
        actor_id="usr_a3f81c2e9b4d",
        session_id="019ea1b9-2c4d-7a6e-b8f0-1a2b3c4d5e6f",
        entity_refs=entity_refs,
        correlation_id="019ea1b9-2c4d-7000-a111-223344556677",
        causation_id="019ea1c5-3a1b-7c2d-9e8f-001122334455",
        op=None,
        payload=payload,
        df=make_canonical_df(),
    )
