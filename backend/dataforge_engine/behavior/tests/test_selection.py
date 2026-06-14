"""Selection, remainder, and guard fall-through (behavior-engine §6.2; §5).

Exercises the §6.2 cumulative-probability table, the remainder rule (``u >= S``
selects the policy), and guard fall-through-WITHOUT-redraw (BE-G2): a failed guard
sends the evaluation to the remainder, never to a different transition, and never
re-draws the selection cursor.
"""

from __future__ import annotations

from dataforge_engine.behavior import compile_manifest
from dataforge_engine.behavior.evaluate import evaluate_guard
from dataforge_engine.behavior.ir import compile_guard
from dataforge_engine.behavior.pools import EntityPools, PooledEntity
from dataforge_engine.behavior.rng import traversal_rng
from dataforge_engine.behavior.runtime import BindingContext, Traversal
from dataforge_engine.seeds import SeedTree

from .fixtures import synthetic_manifest


def test_cumulative_table_is_monotone_and_bounded() -> None:
    ir = compile_manifest(synthetic_manifest())
    browsing = ir.machines["shopping"].states["browsing"]
    cumulatives = [t.cumulative for t in browsing.transitions]
    assert cumulatives == sorted(cumulatives)
    assert browsing.sum_probability <= 1.0 + 1e-9
    # 0.5 + 0.3 = 0.8 ⇒ remainder mass 0.2 belongs to the exit policy.
    assert abs(browsing.sum_probability - 0.8) < 1e-9
    assert browsing.remainder == "exit"


def test_remainder_selected_when_draw_exceeds_sum() -> None:
    """A draw u >= S selects the remainder (index -1), not a transition."""
    ir = compile_manifest(synthetic_manifest())
    browsing = ir.machines["shopping"].states["browsing"]
    # Walk: a u in [0.8, 1.0) must fall past every cumulative ⇒ remainder.
    u = 0.95
    selected = None
    for i, t in enumerate(browsing.transitions):
        if u < t.cumulative:
            selected = i
            break
    assert selected is None  # remainder


def _ctx(pools: EntityPools, subject: PooledEntity, tree: SeedTree) -> BindingContext:
    traversal = Traversal(
        traversal_id="t", machine="order_lifecycle", kind="lifecycle", state="placed",
        actor_key=None, subject_type="orders", subject_key=subject.entity_key,
        rng=traversal_rng(tree, transitions_ctx="t", values_ctx="t"),
        correlation_id="", last_event_id=None,
    )
    return BindingContext(
        pools, actor=None, subject=subject, traversal=traversal,
        now_iso="2026-01-01T00:00:00.000000Z", virtual_epoch_ms=1_767_225_600_000,
    )


def test_guard_passes_when_status_matches() -> None:
    ir = compile_manifest(synthetic_manifest())
    guard = ir.machines["order_lifecycle"].states["placed"].transitions[0].guard
    pools = EntityPools("users")
    pools.register_type("orders")
    order = PooledEntity("ord_1", "orders", {"order_id": "ord_1", "status": "placed"},
                         1, "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z")
    ctx = _ctx(pools, order, SeedTree(1))
    assert evaluate_guard(guard, ctx, pools, 0) is True


def test_guard_fails_when_status_differs_no_cursor_draw() -> None:
    """A failed guard returns False and performs NO draw (BE-G4)."""
    ir = compile_manifest(synthetic_manifest())
    guard = ir.machines["order_lifecycle"].states["placed"].transitions[0].guard
    pools = EntityPools("users")
    pools.register_type("orders")
    order = PooledEntity("ord_1", "orders", {"order_id": "ord_1", "status": "authorized"},
                         1, "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z")
    tree = SeedTree(1)
    traversal = Traversal(
        traversal_id="t", machine="order_lifecycle", kind="lifecycle", state="placed",
        actor_key=None, subject_type="orders", subject_key="ord_1",
        rng=traversal_rng(tree, transitions_ctx="t", values_ctx="t"),
        correlation_id="", last_event_id=None,
    )
    ctx = BindingContext(
        pools, actor=None, subject=order, traversal=traversal,
        now_iso="2026-01-01T00:00:00.000000Z", virtual_epoch_ms=1_767_225_600_000,
    )
    pos_before = traversal.rng.transitions.position
    assert evaluate_guard(guard, ctx, pools, 0) is False
    # Guard evaluation drew nothing from the transitions cursor.
    assert traversal.rng.transitions.position == pos_before


def test_empty_guard_always_true() -> None:
    assert evaluate_guard(compile_guard(None), None, None, 0) is True  # type: ignore[arg-type]
