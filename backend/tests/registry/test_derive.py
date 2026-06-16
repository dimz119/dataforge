"""R-DER schema derivation (schema-registry §5.1; plugin-arch §5.2).

Golden tests against the builtin ecommerce subset: closed documents, all-required,
the §9.2 ``order_placed`` resolution (const/decimal/key patterns, cart_items array
of the remembered object), CDC row images, and byte-identical re-derivation. Pure
logic — no DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from catalog.application import ingest
from registry.infra.canonical import canonical_bytes, comparison_form, fingerprint
from registry.infra.compat import check_backward_additive
from registry.infra.derive import derive_subjects, document_for_version

_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)
_BUILTIN_1_1_0 = _BUILTIN.parent / "1.1.0.yaml"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return ingest.canonicalize(_BUILTIN.read_text(encoding="utf-8")).document


@pytest.fixture(scope="module")
def manifest_1_1_0() -> dict[str, Any]:
    return ingest.canonicalize(_BUILTIN_1_1_0.read_text(encoding="utf-8")).document


def _subject(subjects: list[Any], name: str) -> Any:
    return next(s for s in subjects if s.subject == name)


def test_derives_all_subset_subjects(manifest: dict[str, Any]) -> None:
    subjects = derive_subjects(manifest)
    names = {s.subject for s in subjects}
    # 9 business events + 4 CDC subjects.
    assert len([s for s in subjects if not s.is_cdc]) == 9
    assert len([s for s in subjects if s.is_cdc]) == 4
    assert "ecommerce.order_placed" in names
    assert {"ecommerce.cdc.users", "ecommerce.cdc.products",
            "ecommerce.cdc.orders", "ecommerce.cdc.payments"} <= names


def test_every_document_is_closed_and_all_required(manifest: dict[str, Any]) -> None:
    for s in derive_subjects(manifest):
        doc = s.document
        assert doc["type"] == "object"
        assert doc["additionalProperties"] is False  # R-DER-3
        assert set(doc["required"]) == set(doc["properties"])  # all-required


def test_order_placed_golden(manifest: dict[str, Any]) -> None:
    op = _subject(derive_subjects(manifest), "ecommerce.order_placed")
    props = op.document["properties"]
    assert props["currency"] == {"const": "USD"}
    assert props["total"] == {"type": "string", "pattern": r"^-?\d+\.\d{1,4}$"}
    assert props["order_id"] == {"type": "string", "pattern": "^ord_[0-9a-f]{16}$"}
    assert props["user_id"] == {"type": "string", "pattern": "^usr_[0-9a-f]{16}$"}
    # items: array of the remembered cart-item object (session memory resolution).
    items = props["items"]
    assert items["type"] == "array"
    assert items["items"]["type"] == "object"
    assert items["items"]["additionalProperties"] is False
    assert "product_id" in items["items"]["properties"]


def test_cdc_row_image_has_key_and_timestamps(manifest: dict[str, Any]) -> None:
    users = _subject(derive_subjects(manifest), "ecommerce.cdc.users")
    props = users.document["properties"]
    assert props["user_id"] == {"type": "string", "pattern": "^usr_[0-9a-f]{16}$"}
    assert props["created_at"] == {"type": "string", "format": "date-time"}
    assert props["updated_at"] == {"type": "string", "format": "date-time"}


def test_rederivation_is_byte_identical(manifest: dict[str, Any]) -> None:
    a = {s.subject: canonical_bytes(comparison_form(s.document)) for s in derive_subjects(manifest)}
    b = {s.subject: canonical_bytes(comparison_form(s.document)) for s in derive_subjects(manifest)}
    assert a == b


def test_fingerprint_ignores_annotations(manifest: dict[str, Any]) -> None:
    op = _subject(derive_subjects(manifest), "ecommerce.order_placed")
    annotated = dict(op.document)
    annotated["description"] = "different"
    assert fingerprint(annotated) == fingerprint(op.document)


def test_derivation_order_is_deterministic(manifest: dict[str, Any]) -> None:
    order_a = [s.subject for s in derive_subjects(manifest)]
    order_b = [s.subject for s in derive_subjects(manifest)]
    assert order_a == order_b
    # Business events precede CDC subjects.
    first_cdc = next(i for i, s in enumerate(derive_subjects(manifest)) if s.is_cdc)
    assert all(not s.is_cdc for s in derive_subjects(manifest)[:first_cdc])


# --- REQ-RULE: version-N (N ≥ 2) derivation (schema-registry §4.1/§5.1/§5.3) ----


def test_v2_derivation_adds_field_to_properties_not_required(
    manifest: dict[str, Any], manifest_1_1_0: dict[str, Any]
) -> None:
    """cdc.users v2 (1.1.0 adds ``status``) puts ``status`` in properties, NOT required.

    The normative §1.1 case: deriving the v2 candidate for a subject that already has
    a registered latest (v1) must carry ``required`` forward EXACTLY (REQ-RULE) and
    leave the new field optional, so the §6 BACKWARD_ADDITIVE gate accepts it as an
    additive minor bump. This is the bug the fix closes — the old derivation made
    every property required at every version, tripping REG-C003 on any addition.
    """
    v1 = _subject(derive_subjects(manifest), "ecommerce.cdc.users").document
    derived_v2 = _subject(derive_subjects(manifest_1_1_0), "ecommerce.cdc.users")

    candidate = document_for_version(
        derived_v2,
        latest_required=list(v1["required"]),
        latest_properties=dict(v1["properties"]),
        next_version=2,
    )

    # The new field is present in properties, absent from required.
    assert "status" in candidate["properties"]
    assert "status" not in candidate["required"]
    # REQ-RULE: required(2) == required(1) EXACTLY (carried forward, sorted).
    assert candidate["required"] == sorted(v1["required"])
    assert set(candidate["required"]) == set(v1["required"])
    # Existing fields keep their fragment unchanged (frozen — REG-C002 safe).
    assert candidate["properties"]["user_id"] == v1["properties"]["user_id"]
    # §5.3: the new optional field carries x-df-binding (its manifest declaration).
    assert "x-df-binding" in candidate["properties"]["status"]
    # The versioned header is stamped to v2.
    assert candidate["title"] == "ecommerce.cdc.users v2"
    assert candidate["$id"].endswith("/ecommerce.cdc.users/versions/2.json")


def test_v2_derivation_passes_backward_additive_gate(
    manifest: dict[str, Any], manifest_1_1_0: dict[str, Any]
) -> None:
    """The REQ-RULE v2 candidate is BACKWARD_ADDITIVE-compatible with v1 (empty errors)."""
    v1 = _subject(derive_subjects(manifest), "ecommerce.cdc.users").document
    derived_v2 = _subject(derive_subjects(manifest_1_1_0), "ecommerce.cdc.users")
    candidate = document_for_version(
        derived_v2,
        latest_required=list(v1["required"]),
        latest_properties=dict(v1["properties"]),
        next_version=2,
    )
    assert check_backward_additive(v1, candidate) == []


def test_v2_derivation_with_required_field_would_trip_c003(
    manifest: dict[str, Any], manifest_1_1_0: dict[str, Any]
) -> None:
    """Sanity: a v2 candidate that DOES mark the new field required trips REG-C003.

    Confirms the gate (and the test above) is meaningful — the only thing keeping
    the addition additive is REQ-RULE keeping ``status`` out of ``required``.
    """
    v1 = _subject(derive_subjects(manifest), "ecommerce.cdc.users").document
    derived_v2 = _subject(derive_subjects(manifest_1_1_0), "ecommerce.cdc.users")
    candidate = document_for_version(
        derived_v2,
        latest_required=list(v1["required"]),
        latest_properties=dict(v1["properties"]),
        next_version=2,
    )
    candidate["required"] = sorted([*candidate["required"], "status"])  # violate REQ-RULE
    assert "REG-C003" in {e.code for e in check_backward_additive(v1, candidate)}


def test_unchanged_subject_rederives_byte_identically(manifest: dict[str, Any]) -> None:
    """An UNCHANGED subject re-derives identically (R-DER-4 fingerprint match → no-op).

    The §1.1 ``order_placed`` claim: re-deriving the v2 candidate for a subject whose
    row image did not change yields the v1 comparison form byte-for-byte (no
    new fields → no x-df-binding, required carried forward), so the fingerprint
    matches and Flow 1 registers nothing.
    """
    op = _subject(derive_subjects(manifest), "ecommerce.order_placed").document
    derived = _subject(derive_subjects(manifest), "ecommerce.order_placed")
    candidate = document_for_version(
        derived,
        latest_required=list(op["required"]),
        latest_properties=dict(op["properties"]),
        next_version=2,
    )
    assert fingerprint(candidate) == fingerprint(op)
    assert canonical_bytes(comparison_form(candidate)) == canonical_bytes(comparison_form(op))
