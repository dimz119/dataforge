"""Guard evaluation + value-source resolution (behavior-engine §5; §4 generators).

Guards make invalid sequences structurally impossible: a failed guard falls
through to the remainder policy WITHOUT re-drawing (BE-G2), and guard evaluation
performs no mutation and no draw (BE-G4) so it never perturbs RNG cursors. Value
sources resolve payload fields and effect ``set`` values, drawing from the
traversal's ``values`` cursor for ``generated`` sources (§7.1).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from .distributions import parse_duration_us
from .errors import GenerationError
from .generators import GenContext

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

    from .ir import Comparison, ExistsCondition, Guard, ValueSource
    from .pools import EntityPools
    from .rng import Cursor
    from .runtime import BindingContext


# ---------------------------------------------------------------------------
# Guard evaluation (§5.1).
# ---------------------------------------------------------------------------


def evaluate_guard(
    guard: Guard, ctx: BindingContext, pools: EntityPools, frontier_us: int
) -> bool:
    """``True`` iff every condition passes (a conjunction; BE-G1). No draws (BE-G4)."""
    if guard.is_empty:
        return True
    for comparison in guard.comparisons:
        if not _evaluate_comparison(comparison, ctx, frontier_us):
            return False
    for exists in guard.exists:
        if not _evaluate_exists(exists, ctx, pools, frontier_us):
            return False
    return True


def _evaluate_comparison(comp: Comparison, ctx: BindingContext, frontier_us: int) -> bool:
    actual = ctx.resolve_path(comp.path)
    if comp.op == "within":
        return _within(actual, comp.value, ctx, frontier_us)
    return _compare(comp.op, actual, comp.value)


def _within(
    actual: JSONValue, window: JSONValue, ctx: BindingContext, frontier_us: int
) -> bool:
    """``virtual_frontier - subject.<ts> ≤ window`` (BE-G1; the return window)."""
    if not isinstance(actual, str):
        return False
    from datetime import datetime

    from dataforge_engine.envelope.timestamps import to_epoch_ms

    try:
        ts = datetime.fromisoformat(actual.replace("Z", "+00:00"))
    except ValueError:
        return False
    ts_us = to_epoch_ms(ts) * 1000 - ctx.virtual_epoch_ms() * 1000
    window_us = parse_duration_us(str(window))
    return (frontier_us - ts_us) <= window_us


def _compare(op: str, actual: JSONValue, expected: JSONValue) -> bool:
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return isinstance(expected, list) and actual in expected
    if op == "not_in":
        return isinstance(expected, list) and actual not in expected
    left, right = _numeric(actual), _numeric(expected)
    if left is None or right is None:
        return False
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    raise GenerationError(f"unknown comparison op {op!r}")


def _numeric(value: JSONValue) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ArithmeticError, ValueError):
            return None
    return None


def _evaluate_exists(
    exists: ExistsCondition, ctx: BindingContext, pools: EntityPools, frontier_us: int
) -> bool:
    """O(1) relationship-index lookup with ≤ 4 ``where`` filters (BE-G1)."""
    _of_type, of_record = ctx.resolve_entity_ref(exists.of)
    # exists over relationship where source points at the `of` entity (the target).
    source_keys = pools.sources_for(exists.relationship, of_record.entity_key)
    source_type = pools.relationship_source(exists.relationship)
    matched = False
    for src_key in source_keys:
        record = pools.get(source_type, src_key)
        if record is None or record.status == "deleted":
            continue
        if _where_matches(exists.where, record.attributes, ctx, frontier_us):
            matched = True
            break
    return (not matched) if exists.negate else matched


def _where_matches(
    where: tuple[tuple[str, str, JSONValue | None, str | None], ...],
    attributes: dict[str, JSONValue],
    ctx: BindingContext,
    frontier_us: int,
) -> bool:
    for attribute, op, literal, ref in where:
        actual = attributes.get(attribute)
        expected = ctx.resolve_path(ref) if ref is not None else literal
        if op == "within":
            if not _within(actual, expected, ctx, frontier_us):
                return False
        elif not _compare(op, actual, expected):
            return False
    return True


# ---------------------------------------------------------------------------
# Value-source resolution (payload fields + effect set values).
# ---------------------------------------------------------------------------


def resolve_value_source(
    source: ValueSource,
    ctx: BindingContext,
    cursor: Cursor,
    pools: EntityPools,
    *,
    siblings: dict[str, JSONValue] | None = None,
    ref_keys: dict[str, tuple[str, str]] | None = None,
) -> JSONValue:
    """Resolve one value source (``from``/``const``/``generated``)."""
    if source.kind == "const":
        return source.const
    if source.kind == "from":
        assert source.path is not None
        return ctx.resolve_path(source.path)
    assert source.generator is not None
    gctx = GenContext(
        siblings=siblings if siblings is not None else {},
        pools=pools,
        ref_keys=ref_keys if ref_keys is not None else {},
        expr_resolver=ctx.resolve_path,
    )
    gctx.siblings.setdefault("__now__", ctx.now_iso())
    gctx.siblings.setdefault("__virtual_epoch_ms__", ctx.virtual_epoch_ms())
    return source.generator(cursor, gctx)


def resolve_set(
    sources: tuple[tuple[str, ValueSource], ...],
    ctx: BindingContext,
    cursor: Cursor,
    pools: EntityPools,
) -> dict[str, JSONValue]:
    """Resolve an effect/payload ``set`` mapping in declaration order (R-CDC-2).

    Sibling values accumulate so ``ref.attr`` ``via`` and ``person.email`` ``from``
    see prior fields; ``ref.fk`` results register in ``ref_keys`` for ``ref.attr``.
    """
    resolved: dict[str, JSONValue] = {}
    ref_keys: dict[str, tuple[str, str]] = {}
    for name, source in sources:
        value = resolve_value_source(
            source, ctx, cursor, pools, siblings=resolved, ref_keys=ref_keys
        )
        if value is None and not source.nullable and source.kind == "from":
            raise GenerationError(f"non-nullable field {name!r} resolved to null")
        resolved[name] = value
        if source.kind == "generated" and isinstance(value, str):
            _register_ref(source, name, value, pools, ref_keys)
    return resolved


def _register_ref(
    source: ValueSource, name: str, value: str,
    pools: EntityPools, ref_keys: dict[str, tuple[str, str]],
) -> None:
    # If this generated value is a ref.fk key, record (name → (target_type, key))
    # so a sibling ref.attr via=name can dereference it.
    gen = getattr(source.generator, "__df_relationship__", None)
    if gen is not None:
        ref_keys[name] = (pools.relationship_target(gen), value)
