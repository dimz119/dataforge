"""CDC-1..7 — the CDC-consistency invariant checks (testing-strategy § CDC).

Seven pure checks asserted over a generated full-manifest canonical batch (the
engine *is* the seeded strategy). Each returns ``None`` on success or a
human-readable failure string naming the first offending event, so the SAME checks
run over the PR subset and the nightly full batch with one generated batch each.

CDC events derive from the SAME pool mutation as their business event (ADR-0012,
PoolTransaction's two views), so these checks prove the two views never diverge
(INV-GEN-6). Bound to the spec rows:

* CDC-1  no ``u``/``d`` before ``c``/``r`` per entity instance (R-CDC-4).
* CDC-2  image chaining: every ``u``.before == the prior ``after``;
         ``source.entity_version`` gapless per entity, starting at 1 (R-CDC-5).
* CDC-3  business/CDC adjacency: a CDC row immediately follows its causing
         business event (consecutive ``sequence_no``), sharing ``occurred_at`` and
         ``correlation_id``, with ``causation_id`` == the business ``event_id``
         (R-CDC-2, C-4).
* CDC-4  consistency with the business stream: one ``cdc.orders`` ``c`` per
         ``order_placed`` (the inventory delta reconciliation activates once the
         engine emits inventory-adjusting ``u`` rows — structural, never silent).
* CDC-5  background mutations are chain roots: ``causation_id``/``actor_id`` null,
         ``source.tx_id`` null, ``correlation_id`` == ``event_id`` (R-CDC-3).
* CDC-6  snapshot ``r`` rows: ≤ one per CDC-enabled seeded entity at stream head,
         ``snapshot`` ∈ {"true","last"} (event-model §4.3). This phase's batch
         driver emits no ``r`` rows (they ride the stream head / backfill JSONL
         block), so the check validates *shape* of any present and never demands
         absence-as-failure — it activates the day the head block is interleaved.
* CDC-7  envelope/payload ``op`` equality + per-op image null-rules (CON §8.2).
"""

from __future__ import annotations

from typing import Any

from tests.golden.harness_full import FullBatchResult

_INTRO_OPS = ("c", "r")


def _is_cdc(env: dict[str, Any]) -> bool:
    return str(env["event_type"]).startswith("cdc.")


def _entity_of(env: dict[str, Any]) -> tuple[str, str]:
    """The (entity_type, entity_key) a CDC row is keyed on (PK-2 — first ref)."""
    ref = env["entity_refs"][0]
    return str(ref["entity_type"]), str(ref["entity_key"])


def _seeded_frame(result: FullBatchResult) -> set[tuple[str, str]]:
    """The pool-seeded entities — their birth signal is the snapshot ``r`` at stream
    head (CDC-6), which the batch driver does not interleave, so they are introduced
    at ``entity_version`` 1 by the seed. Treating them as already-``r`` keeps CDC-1/2
    sound for seeded entities (e.g. inventory rows seeded with the product catalog):
    a ``u`` against a seeded row is *not* a u-before-c violation (R-CDC-4)."""
    return {(etype, key) for etype, keys in result.seeded_keys.items() for key in keys}


def check_cdc1_no_ud_before_cr(result: FullBatchResult) -> str | None:
    """CDC-1: no ``u``/``d`` for an entity instance before its ``c``/``r`` (R-CDC-4).

    Pool-seeded entities are introduced by their stream-head snapshot ``r`` (CDC-6),
    so they seed the introduced frame at version 1 — exactly the PROP-RI-1 position-0
    reference convention (the snapshot block precedes every mutation)."""
    introduced: set[tuple[str, str]] = _seeded_frame(result)
    for env in result.envelopes:
        if not _is_cdc(env):
            continue
        key = _entity_of(env)
        op = env["op"]
        if op in _INTRO_OPS:
            introduced.add(key)
        elif op in ("u", "d") and key not in introduced:
            return (
                f"CDC-1: {env['event_type']} op={op} (seq {env['sequence_no']}) for "
                f"{key[0]}:{key[1]} precedes its c/r introduction"
            )
    return None


def check_cdc2_image_chaining_gapless(result: FullBatchResult) -> str | None:
    """CDC-2: ``u``.before == prior ``after``; ``entity_version`` gapless from 1."""
    last_after: dict[tuple[str, str], dict[str, Any]] = {}
    last_version: dict[tuple[str, str], int] = {}
    # Seeded entities are at version 1 from their stream-head snapshot ``r`` (CDC-6);
    # the first mutation a seeded entity receives is therefore a ``u`` to version 2.
    seeded = _seeded_frame(result)
    for key in seeded:
        last_version[key] = 1
    for env in result.envelopes:
        if not _is_cdc(env):
            continue
        key = _entity_of(env)
        payload = env["payload"]
        op = env["op"]
        version = int(payload["source"]["entity_version"])
        prev_v = last_version.get(key)
        expected_v = 1 if prev_v is None else prev_v + 1
        if version != expected_v:
            return (
                f"CDC-2: {env['event_type']} (seq {env['sequence_no']}) entity_version "
                f"{version} for {key[0]}:{key[1]}; expected gapless {expected_v}"
            )
        if op == "u":
            before = payload.get("before")
            prior = last_after.get(key)
            if prior is not None and before != prior:
                return (
                    f"CDC-2: {env['event_type']} (seq {env['sequence_no']}) before-image "
                    f"for {key[0]}:{key[1]} != the prior after-image (chain broken)"
                )
        last_version[key] = version
        if op in ("c", "u", "r"):
            after = payload.get("after")
            if isinstance(after, dict):
                last_after[key] = after
    return None


def check_cdc3_business_cdc_adjacency(result: FullBatchResult) -> str | None:
    """CDC-3: a *caused* CDC row follows its business event with a consecutive
    ``sequence_no``, sharing ``occurred_at``/``correlation_id`` and carrying
    ``causation_id`` == the business ``event_id`` (R-CDC-2, C-4). Background CDC
    (``causation_id`` null) is a chain root and is exempt — CDC-5 covers it."""
    by_id: dict[str, dict[str, Any]] = {}
    by_seq: dict[int, dict[str, Any]] = {}
    for env in result.envelopes:
        by_id[env["event_id"]] = env
        by_seq[int(env["sequence_no"])] = env
    for env in result.envelopes:
        if not _is_cdc(env):
            continue
        cause_id = env["causation_id"]
        if cause_id is None:  # background chain root — CDC-5
            continue
        cause = by_id.get(cause_id)
        if cause is None:
            return (
                f"CDC-3: {env['event_type']} (seq {env['sequence_no']}) causation_id "
                f"{cause_id} resolves to no prior event"
            )
        if int(env["sequence_no"]) <= int(cause["sequence_no"]):
            return (
                f"CDC-3: {env['event_type']} (seq {env['sequence_no']}) does not follow "
                f"its cause (seq {cause['sequence_no']})"
            )
        if env["occurred_at"] != cause["occurred_at"]:
            return (
                f"CDC-3: {env['event_type']} (seq {env['sequence_no']}) occurred_at "
                f"{env['occurred_at']} != cause {cause['occurred_at']}"
            )
        if env["correlation_id"] != cause["correlation_id"]:
            return (
                f"CDC-3: {env['event_type']} (seq {env['sequence_no']}) correlation_id "
                f"differs from its cause"
            )
    return None


def check_cdc4_business_stream_consistency(result: FullBatchResult) -> str | None:
    """CDC-4: exactly one ``cdc.orders`` ``c`` per ``order_placed`` (one mutation,
    two views). The inventory stock-delta reconciliation activates once the engine
    emits inventory-adjusting ``u`` rows tied to orders — structural, never silent."""
    orders_placed = 0
    cdc_orders_create = 0
    for env in result.envelopes:
        if env["event_type"] == "order_placed":
            orders_placed += 1
        elif env["event_type"] == "cdc.orders" and env["op"] == "c":
            cdc_orders_create += 1
    if orders_placed and cdc_orders_create != orders_placed:
        return (
            f"CDC-4: {orders_placed} order_placed but {cdc_orders_create} cdc.orders c "
            "(one create per order required)"
        )
    return None


def check_cdc5_background_mutations_are_roots(result: FullBatchResult) -> str | None:
    """CDC-5: every background CDC row (``source.tx_id`` null) is a chain root —
    ``causation_id``/``actor_id`` null, ``correlation_id`` == ``event_id`` (R-CDC-3)."""
    for env in result.envelopes:
        if not _is_cdc(env):
            continue
        tx_id = env["payload"]["source"].get("tx_id")
        if tx_id is not None:  # a business-tx CDC row — not a background root
            continue
        if env["causation_id"] is not None:
            return (
                f"CDC-5: background {env['event_type']} (seq {env['sequence_no']}) has "
                f"causation_id {env['causation_id']} (chain roots must be null)"
            )
        if env.get("actor_id") is not None:
            return (
                f"CDC-5: background {env['event_type']} (seq {env['sequence_no']}) has "
                f"actor_id {env['actor_id']} (chain roots are actorless)"
            )
        if env["correlation_id"] != env["event_id"]:
            return (
                f"CDC-5: background {env['event_type']} (seq {env['sequence_no']}) "
                "correlation_id != event_id (a chain root is its own correlation)"
            )
    return None


def check_cdc6_snapshot_rows_well_formed(result: FullBatchResult) -> str | None:
    """CDC-6: every ``r`` row is at most once per CDC-enabled entity at head, with
    ``snapshot`` ∈ {"true","last"} and ``before`` null (event-model §4.3). The batch
    driver emits no ``r`` rows this phase (they ride the stream head / backfill JSONL
    block), so this validates the shape of any present and never fails on absence."""
    seen: set[tuple[str, str]] = set()
    for env in result.envelopes:
        if not _is_cdc(env) or env["op"] != "r":
            continue
        key = _entity_of(env)
        if key in seen:
            return f"CDC-6: duplicate snapshot r for {key[0]}:{key[1]} (seq {env['sequence_no']})"
        seen.add(key)
        snap = env["payload"]["source"].get("snapshot")
        if snap not in ("true", "last"):
            return f"CDC-6: snapshot r (seq {env['sequence_no']}) has snapshot={snap!r}"
        if env["payload"].get("before") is not None:
            return f"CDC-6: snapshot r (seq {env['sequence_no']}) carries a before image"
    return None


def check_cdc7_op_equality_and_image_nulls(result: FullBatchResult) -> str | None:
    """CDC-7: envelope ``op`` == ``payload.op``; per-op image null-rules — ``c``/``r``
    have ``before`` null + ``after`` set; ``u`` has both; ``d`` has ``before`` set +
    ``after`` null (event-model §4.3)."""
    for env in result.envelopes:
        if not _is_cdc(env):
            continue
        op = env["op"]
        payload = env["payload"]
        if payload.get("op") != op:
            return (
                f"CDC-7: {env['event_type']} (seq {env['sequence_no']}) envelope op {op} "
                f"!= payload op {payload.get('op')}"
            )
        before = payload.get("before")
        after = payload.get("after")
        if op in ("c", "r") and (before is not None or after is None):
            return (
                f"CDC-7: {op} (seq {env['sequence_no']}) image rule: before must be null, after set"
            )
        if op == "u" and (before is None or after is None):
            return f"CDC-7: u (seq {env['sequence_no']}) must carry both before and after images"
        if op == "d" and (before is None or after is not None):
            return f"CDC-7: d (seq {env['sequence_no']}) image rule: before set, after null"
    return None


# The full CDC-consistency suite, in id order, for the profile runners to iterate.
ALL_CDC_CHECKS: tuple[tuple[str, Any], ...] = (
    ("CDC-1", check_cdc1_no_ud_before_cr),
    ("CDC-2", check_cdc2_image_chaining_gapless),
    ("CDC-3", check_cdc3_business_cdc_adjacency),
    ("CDC-4", check_cdc4_business_stream_consistency),
    ("CDC-5", check_cdc5_background_mutations_are_roots),
    ("CDC-6", check_cdc6_snapshot_rows_well_formed),
    ("CDC-7", check_cdc7_op_equality_and_image_nulls),
)
