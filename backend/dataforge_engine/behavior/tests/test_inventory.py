"""Inventory never negative — check-and-adjust atomicity (behavior-engine §5.3).

The synthetic manifest decrements ``products.stock`` on each order; seeded stock
is fixed at 5. The absolute floor (BE-G6) must hold: no product ever goes below 0,
and an ``adjust`` that would breach the floor aborts its whole transaction
(BE-G5), falling to the remainder exactly as a guard failure — never clamping.
"""

from __future__ import annotations

from decimal import Decimal

from dataforge_engine.behavior import GenerationError, Shard, ShardConfig, compile_manifest
from dataforge_engine.behavior.pools import EntityPools, PooledEntity
from dataforge_engine.behavior.transaction import StreamIdentity

from .fixtures import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WORKSPACE_ID,
    FixedWallClock,
    synthetic_manifest,
)


def test_no_product_stock_below_zero_after_a_full_run() -> None:
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=7, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    shard = Shard(ir, config, FixedWallClock())
    shard.run_batch(max_events=2000)
    for pid in shard.pools.live_keys("products"):
        stock = shard.pools.require("products", pid).attributes["stock"]
        assert isinstance(stock, int)
        assert stock >= 0


def test_adjust_below_floor_raises_generation_error() -> None:
    """A direct adjust below 0 raises GenerationError (the floor, BE-G6)."""
    from datetime import UTC, datetime

    from dataforge_engine.behavior.clock import VirtualClock
    from dataforge_engine.behavior.interpreter import Interpreter
    from dataforge_engine.behavior.ir import Effect
    from dataforge_engine.behavior.rng import traversal_rng
    from dataforge_engine.behavior.runtime import BindingContext, Traversal
    from dataforge_engine.behavior.scheduler import TimerHeap
    from dataforge_engine.behavior.transaction import PoolTransaction, SequenceCounter
    from dataforge_engine.seeds import SeedTree

    ir = compile_manifest(synthetic_manifest())
    pools = EntityPools("users")
    pools.register_type("products")
    pools.insert(PooledEntity("prd_1", "products", {"product_id": "prd_1", "stock": 0},
                              1, "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z"))
    tree = SeedTree(1)
    clock = VirtualClock(virtual_epoch=VIRTUAL_EPOCH)
    interp = Interpreter(
        ir, pools, TimerHeap(),
        StreamIdentity(WORKSPACE_ID, STREAM_ID, 0, "synth", "1.0.0"),
        SequenceCounter(), clock, tree,
    )
    traversal = Traversal(
        traversal_id="t", machine="shopping", kind="session", state="checkout",
        actor_key=None, subject_type="products", subject_key="prd_1",
        rng=traversal_rng(tree, transitions_ctx="t", values_ctx="t"),
        correlation_id="", last_event_id=None,
    )
    occ = datetime(2026, 1, 1, tzinfo=UTC)
    ctx = BindingContext(
        pools, actor=None, subject=pools.get("products", "prd_1"), traversal=traversal,
        now_iso="2026-01-01T00:00:00.000000Z", virtual_epoch_ms=clock.virtual_epoch_ms,
    )
    tx = PoolTransaction(ir, StreamIdentity(WORKSPACE_ID, STREAM_ID, 0, "synth", "1.0.0"),
                         occurred_at=occ, emitted_at=occ)
    effect = Effect("adjust", target="subject", attribute="stock", by_const=-1.0)
    try:
        interp._effect_adjust(effect, ctx, traversal, tx, occ)
    except GenerationError:
        pass
    else:
        raise AssertionError("adjust below floor must raise GenerationError")
    # The pool was NOT mutated (no clamp, no partial change).
    assert pools.require("products", "prd_1").attributes["stock"] == 0


def test_decimal_money_stays_decimal() -> None:
    """Money attributes resolve to Decimal so the serializer renders strings (S-6)."""
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=7, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    shard = Shard(ir, config, FixedWallClock())
    shard.run_batch(max_events=300)
    for oid in shard.pools.live_keys("orders"):
        total = shard.pools.require("orders", oid).attributes["total"]
        assert isinstance(total, Decimal)
