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
from registry.infra.derive import derive_subjects

_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return ingest.canonicalize(_BUILTIN.read_text(encoding="utf-8")).document


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
