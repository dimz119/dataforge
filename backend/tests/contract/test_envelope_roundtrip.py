"""Envelope round-trip + artifact-validation contract tests (event-model §2, EV-6).

These exercise the envelope as the *published language*: build an internal
envelope with ``schema_ref`` stamped, serialize canonically, validate the
serialized bytes against the committed envelope 1.0 JSON Schema artifact, and
parse back byte-identically. Pure Python (no DB) — run in every lane.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataforge_engine.envelope import (
    canonical_serialize,
    event_id_for,
    strip_internal,
    validate_against_schema,
    validate_envelope,
)
from dataforge_engine.envelope.tests.fixtures import (
    SeededRandomBits,
    order_placed_envelope,
)

# The committed CI artifact (event-model EV-6).
_ARTIFACT = Path(__file__).resolve().parents[2] / "schema" / "envelope-1.0.schema.json"


def _load_artifact() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    return loaded


def test_artifact_exists() -> None:
    assert _ARTIFACT.exists(), (
        f"{_ARTIFACT} missing — run `manage.py generate_envelope_schema` and commit it (EV-6)."
    )


def test_order_placed_schema_ref_stamped() -> None:
    env = order_placed_envelope()
    assert env["schema_ref"] == {"subject": "ecommerce.order_placed", "version": 1}


def test_delivered_envelope_validates_against_in_memory_schema() -> None:
    """The stripped delivered envelope validates against the generated schema."""
    validate_envelope(strip_internal(order_placed_envelope()))


def test_internal_envelope_rejected_by_closed_schema() -> None:
    """The frame schema is closed (additionalProperties:false): an *un*-stripped
    internal envelope (carrying ``_df``) is rejected — so the strip is precisely
    what produces a wire-valid document, locking the §5.2 boundary contract.
    """
    from dataforge_engine.envelope import EnvelopeSchemaError

    try:
        validate_envelope(order_placed_envelope())
    except EnvelopeSchemaError as exc:
        assert "_df" in str(exc)
    else:  # pragma: no cover - the closed schema must reject _df
        raise AssertionError("internal envelope with _df must fail the closed frame schema")


def test_delivered_envelope_validates_against_committed_artifact() -> None:
    """The serialized delivered sample validates against the on-disk artifact."""
    delivered = strip_internal(order_placed_envelope())
    validate_against_schema(delivered, _load_artifact())


def test_canonical_serialize_then_parse_round_trips_byte_identically() -> None:
    """Serialize → parse → re-serialize yields byte-identical bytes."""
    delivered = strip_internal(order_placed_envelope())
    first = canonical_serialize(delivered)
    parsed = json.loads(first.decode("utf-8"))
    # Re-serialize the parsed dict; JSON parse preserves object key order, and the
    # serializer renders top-level fields in catalog order, so bytes must match.
    second = canonical_serialize(parsed)
    assert first == second


def test_determinism_same_seed_and_occurred_at() -> None:
    """Same (seed, occurred_at) → same event_id → byte-identical envelope."""
    a = canonical_serialize(order_placed_envelope(seed=4242))
    b = canonical_serialize(order_placed_envelope(seed=4242))
    assert a == b


def test_determinism_event_id_independent_of_repeat() -> None:
    occ = datetime(2026, 6, 10, 14, 23, 5, 123456, tzinfo=UTC)
    assert event_id_for(occ, SeededRandomBits(4242)) == event_id_for(occ, SeededRandomBits(4242))
