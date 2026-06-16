"""Generalized guard vocabulary (behavior-engine §5; §6.2 rule 3; §6.3).

The Phase-8 full manifest needs three guard forms beyond a bare attribute ``eq``,
all already in the §5 vocabulary and exercised here against the live engine:

* **relationship-existence** (BE-G1 ``exists``) — the refund gate: a refund is
  structurally impossible until a *delivered/lost* shipment exists for the order
  (§5.2). O(1) relationship-index lookup, never a scan.
* **attribute comparison** — the comparison ops (``eq/ne/gt/.../in/not_in``).
* **virtual-clock window** (``within``) — the return window:
  ``virtual_frontier - subject.delivered_at <= P30D`` (§6.3), in simulated time.

and the §6.2 rule-3 fall-through: a false guard sends the evaluation to the
remainder policy WITHOUT re-drawing the selection cursor (BE-G2/G4) — guards
mutate nothing and draw nothing, so tightening a guard never scrambles RNG.

Pure Python; no Django (BE-ENG-1).
"""

from __future__ import annotations

from dataforge_engine.behavior.evaluate import evaluate_guard
from dataforge_engine.behavior.ir import (
    Comparison,
    ExistsCondition,
    Guard,
    compile_guard,
)
from dataforge_engine.behavior.pools import EntityPools, PooledEntity
from dataforge_engine.behavior.rng import traversal_rng
from dataforge_engine.behavior.runtime import BindingContext, Traversal
from dataforge_engine.seeds import SeedTree

# 2026-01-01T00:00:00Z as integer epoch ms (the synthetic virtual epoch).
EPOCH_MS = 1_767_225_600_000
_ISO = "2026-01-01T00:00:00.000000Z"
_DAY_US = 86_400 * 1_000_000


def _order_pools() -> EntityPools:
    pools = EntityPools("users")
    for entity_type in ("users", "products", "orders", "shipments"):
        pools.register_type(entity_type)
    # The refund gate's relationship: shipments.order_id → orders (§5.2).
    pools.register_relationship("shipment_order", "shipments", "order_id", "orders")
    return pools


def _ctx(pools: EntityPools, subject: PooledEntity) -> BindingContext:
    tree = SeedTree(1)
    traversal = Traversal(
        traversal_id="t", machine="shipment_lifecycle", kind="lifecycle",
        state="delivered", actor_key=None, subject_type=subject.entity_type,
        subject_key=subject.entity_key,
        rng=traversal_rng(tree, transitions_ctx="t", values_ctx="t"),
        correlation_id="", last_event_id=None,
    )
    return BindingContext(
        pools, actor=None, subject=subject, traversal=traversal,
        now_iso=_ISO, virtual_epoch_ms=EPOCH_MS,
    )


def _order(pools: EntityPools, status: str = "completed") -> PooledEntity:
    order = PooledEntity(
        "ord_1", "orders", {"order_id": "ord_1", "status": status},
        1, _ISO, _ISO,
    )
    pools.insert(order)
    return order


def _add_shipment(pools: EntityPools, status: str) -> PooledEntity:
    ship = PooledEntity(
        "shp_1", "shipments",
        {"shipment_id": "shp_1", "order_id": "ord_1", "status": status},
        1, _ISO, _ISO,
    )
    pools.insert(ship)
    return ship


# ---------------------------------------------------------------------------
# (1) relationship-existence — the refund gate blocks, then allows (§5.2).
# ---------------------------------------------------------------------------


def _refund_gate() -> Guard:
    # exists a shipment of this order whose status ∈ {delivered, lost}.
    return compile_guard({"all": [{"exists": {
        "relationship": "shipment_order", "of": "subject",
        "where": [{"attribute": "status", "op": "in",
                   "value": ["delivered", "lost"]}],
    }}]})


def test_exists_guard_blocks_without_a_delivered_shipment() -> None:
    pools = _order_pools()
    order = _order(pools)
    guard = _refund_gate()
    # No shipment row at all ⇒ the refund edge is unrepresentable (INV-GEN-2).
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is False
    # An in-transit shipment exists but is not delivered/lost ⇒ still blocked.
    _add_shipment(pools, "in_transit")
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is False


def test_exists_guard_allows_once_a_delivered_shipment_exists() -> None:
    pools = _order_pools()
    order = _order(pools)
    ship = _add_shipment(pools, "in_transit")
    guard = _refund_gate()
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is False
    # Delivery flips the same indexed row delivered ⇒ the gate opens.
    ship.attributes["status"] = "delivered"
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is True
    # The "lost" terminal outcome also satisfies the gate (auto-refund path).
    ship.attributes["status"] = "lost"
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is True


def test_exists_negate_inverts_the_gate() -> None:
    pools = _order_pools()
    order = _order(pools)
    guard = Guard(
        comparisons=(),
        exists=(ExistsCondition(
            relationship="shipment_order", of="subject", negate=True,
            where=(("status", "in", ["delivered", "lost"], None),),
        ),),
    )
    # No delivered shipment ⇒ negate makes the guard TRUE.
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is True
    _add_shipment(pools, "delivered")
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is False


# ---------------------------------------------------------------------------
# (2) attribute comparison — the comparison ops (§6.3).
# ---------------------------------------------------------------------------


def test_attribute_comparison_ops() -> None:
    pools = _order_pools()
    order = _order(pools, status="completed")
    order.attributes["total"] = 120
    ctx = _ctx(pools, order)

    def check(op: str, value: object, path: str = "subject.status") -> bool:
        guard = Guard((Comparison(path, op, value),), ())  # type: ignore[arg-type]
        return evaluate_guard(guard, ctx, pools, 0)

    assert check("eq", "completed") is True
    assert check("ne", "placed") is True
    assert check("in", ["completed", "cancelled"]) is True
    assert check("not_in", ["placed", "cancelled"]) is True
    # numeric comparisons coerce ints/Decimals/strings uniformly.
    assert check("gte", 100, path="subject.total") is True
    assert check("gt", 200, path="subject.total") is False
    assert check("lte", 120, path="subject.total") is True
    assert check("lt", 100, path="subject.total") is False


def test_comparison_against_a_missing_path_is_false_not_an_error() -> None:
    pools = _order_pools()
    order = _order(pools)
    ctx = _ctx(pools, order)
    # A numeric op over an absent attribute resolves to None ⇒ guard False (BE-G1),
    # never a draw, never a raise.
    guard = Guard((Comparison("subject.refund_total", "gt", 0),), ())
    assert evaluate_guard(guard, ctx, pools, 0) is False


# ---------------------------------------------------------------------------
# (3) virtual-clock window — the return window (`within`; §6.3).
# ---------------------------------------------------------------------------


def test_within_window_open_inside_then_closed_outside() -> None:
    pools = _order_pools()
    order = _order(pools)
    # delivered exactly at the virtual epoch; `within` compares the firing
    # frontier against subject.delivered_at in simulated time.
    order.attributes["delivered_at"] = _ISO
    guard = compile_guard({"all": [
        {"path": "subject.delivered_at", "op": "within", "value": "P30D"}]})

    # 4 simulated days after delivery ⇒ inside the 30-day return window.
    assert evaluate_guard(guard, _ctx(pools, order), pools, 4 * _DAY_US) is True
    # the boundary day is inclusive (`≤ window`, BE-G1).
    assert evaluate_guard(guard, _ctx(pools, order), pools, 30 * _DAY_US) is True
    # 31 days ⇒ the window is closed; the refund edge falls through forever.
    assert evaluate_guard(guard, _ctx(pools, order), pools, 31 * _DAY_US) is False


def test_within_window_rejects_a_non_timestamp_value() -> None:
    pools = _order_pools()
    order = _order(pools)  # no delivered_at attribute set at all
    guard = compile_guard({"all": [
        {"path": "subject.delivered_at", "op": "within", "value": "P30D"}]})
    # F-6: delivered_at is a placeholder before delivery; a non-string/None value
    # makes the window guard False (the edge is only evaluated from `delivered`).
    assert evaluate_guard(guard, _ctx(pools, order), pools, 0) is False


# ---------------------------------------------------------------------------
# (4) guard-false falls through to the remainder WITHOUT re-drawing (BE-G2/G4).
# ---------------------------------------------------------------------------


def test_false_guard_draws_nothing_from_either_cursor() -> None:
    pools = _order_pools()
    order = _order(pools)
    ctx = _ctx(pools, order)
    traversal = ctx._traversal  # the same traversal the ctx was built around
    # A conjunction mixing all three forms, all false (no shipment, window closed,
    # status mismatch) — the most work the evaluator can do before returning False.
    guard = compile_guard({"all": [
        {"path": "subject.status", "op": "eq", "value": "placed"},
        {"path": "subject.delivered_at", "op": "within", "value": "P30D"},
        {"exists": {"relationship": "shipment_order", "of": "subject",
                    "where": [{"attribute": "status", "op": "in",
                               "value": ["delivered", "lost"]}]}},
    ]})
    tpos = traversal.rng.transitions.position
    vpos = traversal.rng.values.position
    assert evaluate_guard(guard, ctx, pools, 5 * _DAY_US) is False
    # BE-G4: guard evaluation perturbs neither RNG cursor, so the remainder is
    # reached with the selection draw intact — no re-draw, no retry (BE-G2).
    assert traversal.rng.transitions.position == tpos
    assert traversal.rng.values.position == vpos


def test_passing_guard_also_draws_nothing() -> None:
    pools = _order_pools()
    order = _order(pools)
    _add_shipment(pools, "delivered")
    order.attributes["delivered_at"] = _ISO
    ctx = _ctx(pools, order)
    traversal = ctx._traversal
    guard = _refund_gate()
    tpos = traversal.rng.transitions.position
    vpos = traversal.rng.values.position
    assert evaluate_guard(guard, ctx, pools, 2 * _DAY_US) is True
    assert traversal.rng.transitions.position == tpos
    assert traversal.rng.values.position == vpos
