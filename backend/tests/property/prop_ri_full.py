"""PROP-RI-1..8 over the FULL manifest (ecommerce 1.1.0 + CDC) — the Phase-8 profile.

Phase 8's exit criterion #1 / #8 is "referential validity over a 1M-event batch of
the **full** manifest." The eight PROP-RI invariants are identical in *meaning* to
the subset profile (:mod:`tests.property.checks`), but two of them gain their full
contract here because the full manifest emits the downstream funnel + inventory CDC:

* PROP-RI-3 — **no refund without a delivered or lost shipment**: every
  ``refund_requested`` is preceded (same order) by ``shipment_delivered`` or
  ``shipment_lost`` within the window (PRD F9 gate). The subset proved only the
  order→payment edge; the full manifest proves the refund gate.
* PROP-RI-4 — **inventory never negative + reconciliation**: replaying every
  ``cdc.inventory`` ``u`` image, ``stock`` is never negative at any version.

The remaining six (RI-1/2/5/6/7/8) are reused verbatim from the subset checks —
they are manifest-agnostic — with a full-manifest ``schema_ref`` subject frame for
RI-8. Pure engine + ports (no Postgres, no Redis): one generated batch feeds all
eight. This module never special-cases e-commerce in the *runtime* — it is a test
asserting the manifest DATA produced a referentially-valid stream.
"""

from __future__ import annotations

from typing import Any

from tests.golden.harness_full import FullBatchResult, full_ecommerce_document
from tests.property import checks as subset

# Refund gate: a refund_requested must be preceded by one of these for its order.
_SHIPMENT_TERMINALS = ("shipment_delivered", "shipment_lost")


def _full_subjects(overlay: dict[str, Any] | None = None) -> set[str]:
    """The registry subjects the FULL manifest registers (business + cdc.{entity})."""
    document = full_ecommerce_document(overlay)
    slug = document["metadata"]["slug"]
    subjects = {f"{slug}.{name}" for name in document.get("event_types", {})}
    cdc = (document.get("cdc") or {}).get("entities", {})
    subjects |= {f"{slug}.cdc.{entity}" for entity in cdc}
    return subjects


def check_ri3_refund_requires_shipment(result: FullBatchResult) -> str | None:
    """PROP-RI-3 (full): no ``refund_requested`` without a prior ``shipment_delivered``
    or ``shipment_lost`` for its order (PRD F9 gate). Activates the day the engine
    emits the shipment terminals — structural, never silently passing on a violation."""
    terminalled_orders: set[str] = set()
    for env in result.envelopes:
        et = env["event_type"]
        if et in _SHIPMENT_TERMINALS:
            oid = env["payload"].get("order_id")
            if oid is not None:
                terminalled_orders.add(str(oid))
        elif et == "refund_requested":
            oid = str(env["payload"].get("order_id"))
            if oid not in terminalled_orders:
                return (
                    f"PROP-RI-3: refund_requested (seq {env['sequence_no']}) for order "
                    f"{oid} with no prior shipment_delivered/shipment_lost"
                )
    return None


def check_ri4_inventory_reconciles(result: FullBatchResult) -> str | None:
    """PROP-RI-4 (full): replaying every ``cdc.inventory`` ``u``/``c`` ``after`` image,
    ``stock`` is never negative at any version (PRD §4.4). Reuses the subset check's
    image walk — the full manifest gives it real inventory CDC to validate."""
    return subset.check_ri4_inventory_never_negative(result)  # type: ignore[arg-type]


def check_ri8_schema_ref_full(result: FullBatchResult) -> str | None:
    """PROP-RI-8 over the FULL subject frame (business + 8 cdc.{entity} subjects)."""
    from dataforge_engine.envelope import DELIVERED_FIELD_SET

    subjects = _full_subjects(result.overlay)
    for env in result.envelopes:
        ref = env["schema_ref"]
        if ref["subject"] not in subjects:
            return (
                f"PROP-RI-8: schema_ref subject {ref['subject']!r} (seq "
                f"{env['sequence_no']}) is not a registered full-manifest subject"
            )
        if ref["version"] != 1:
            return (
                f"PROP-RI-8: schema_ref version {ref['version']} != 1 at seq {env['sequence_no']}"
            )
        delivered = {k for k in env if k != "_df"}
        if delivered != DELIVERED_FIELD_SET:
            missing = DELIVERED_FIELD_SET - delivered
            extra = delivered - DELIVERED_FIELD_SET
            return (
                f"PROP-RI-8: envelope key-set drift at seq {env['sequence_no']}: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
    return None


# The full PROP-RI suite over the full manifest, in id order. RI-1/2/5/6/7 reuse the
# manifest-agnostic subset checks; RI-3/4/8 use the full-manifest variants above.
ALL_FULL_CHECKS: tuple[tuple[str, Any], ...] = (
    ("PROP-RI-1", subset.check_ri1_entity_refs_resolve),
    ("PROP-RI-2", subset.check_ri2_payment_requires_order),
    ("PROP-RI-3", check_ri3_refund_requires_shipment),
    ("PROP-RI-4", check_ri4_inventory_reconciles),
    ("PROP-RI-5", subset.check_ri5_sequence_gapless),
    ("PROP-RI-6", subset.check_ri6_occurred_at_monotone),
    ("PROP-RI-7", subset.check_ri7_causality_resolves),
    ("PROP-RI-8", check_ri8_schema_ref_full),
)
