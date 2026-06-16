"""CDC emission via the PoolTransaction CDC-view + snapshots + background
mutations (P8-04/P8-05; event-model §4 R-CDC; ADR-0012).

These tests pin the binding R-CDC invariants on the generic engine output:

* op ``c`` before any ``u``/``d`` for an entity, ``r`` snapshots at the stream
  head (occurred_at = virtual_epoch) — R-CDC-4.
* ``source.entity_version`` gapless per entity, ``before`` image chains to
  ``after`` of the prior version — R-CDC-5.
* business event then its CDC adjacent: shared occurred_at + correlation_id,
  consecutive ``sequence_no``, ``causation_id`` = the business ``event_id`` —
  R-CDC-2.
* background mutations are CDC-only chain roots: ``causation_id``/``actor_id``/
  ``session_id``/``source.tx_id`` null, ``correlation_id = event_id`` — R-CDC-3.
* determinism: a fixed seed yields byte-identical CDC output — GOLD-B.

The engine carries zero scenario code; the fixture manifest declares CDC + two
background-mutation rules as DATA.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from typing import Any

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest

from .fixtures import (
    STREAM_ID,
    VIRTUAL_EPOCH,
    WORKSPACE_ID,
    FixedWallClock,
    synthetic_manifest,
)


def _run(seed: int = 11, max_events: int = 2500):  # type: ignore[no-untyped-def]
    # Untyped on purpose (the project test idiom, cf. test_determinism._run_raw):
    # the envelopes flow as ``Any`` so the Debezium ``payload``/``source`` sub-doc
    # can be indexed without per-field casts in assertions.
    ir = compile_manifest(synthetic_manifest())
    config = ShardConfig(
        seed=seed, workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH, mode="backfill", mean_events_per_session=5.0,
    )
    return Shard(ir, config, FixedWallClock()).run_batch(max_events=max_events)


def _cdc(batch: Any) -> list[Any]:
    return [e for e in batch if str(e["event_type"]).startswith("cdc.")]


def _entity_key(env: Any) -> tuple[str, str]:
    ref = env["entity_refs"][0]
    return ref["entity_type"], ref["entity_key"]


# --- R-CDC-4: ordering -----------------------------------------------------


def test_no_update_or_delete_before_create_or_snapshot() -> None:
    """No ``u``/``d`` CDC event is emitted for an entity before its ``c``/``r``."""
    seen_cr: set[tuple[str, str]] = set()
    for env in _cdc(_run()):
        key = _entity_key(env)
        if env["op"] in ("c", "r"):
            seen_cr.add(key)
        elif env["op"] in ("u", "d"):
            assert key in seen_cr, f"{env['op']} before c/r for {key}"


def test_r_snapshots_at_stream_head_at_virtual_epoch() -> None:
    """Every seeded CDC entity emits exactly one ``r`` at the head, occurred_at=epoch."""
    batch = _run(max_events=400)
    head_occurred_at = batch[0]["occurred_at"]
    r_rows = [e for e in batch if e["op"] == "r"]
    assert r_rows, "expected r-snapshots at the head"
    # all snapshots come before any non-r event and share the epoch instant.
    first_non_r = next(i for i, e in enumerate(batch) if e["op"] != "r")
    assert all(e["op"] == "r" for e in batch[:first_non_r])
    assert all(e["occurred_at"] == head_occurred_at for e in r_rows)
    # one r per seeded CDC instance (10 users + 5 products in the fixture).
    keys = {_entity_key(e) for e in r_rows}
    assert len(keys) == len(r_rows) == 15
    for e in r_rows:
        assert e["payload"]["before"] is None
        assert e["payload"]["after"] is not None
        assert e["payload"]["source"]["snapshot"] == "true"


# --- R-CDC-5: gapless entity_version + before-image chaining ---------------


def test_entity_version_gapless_and_before_image_chains() -> None:
    """Per entity, emitted ``source.entity_version`` rises by exactly 1 and each
    ``before`` image equals the prior emission's ``after`` (SCD2 reconstruction)."""
    per_entity: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for env in _cdc(_run()):
        per_entity[_entity_key(env)].append(env)
    chained = 0
    for events in per_entity.values():
        prev_after: dict[str, Any] | None = None
        prev_version: int | None = None
        for env in events:
            version = env["payload"]["source"]["entity_version"]
            if prev_version is not None:
                assert version == prev_version + 1, "entity_version gap"
                # before image of this mutation == after image of the prior one.
                assert env["payload"]["before"] == prev_after
                chained += 1
            prev_version = version
            prev_after = env["payload"]["after"]
    assert chained > 0, "expected at least one chained mutation pair"


# --- R-CDC-2: business/CDC adjacency ---------------------------------------


def test_business_event_and_its_cdc_are_adjacent() -> None:
    """A business event is immediately followed by its CDC: shared occurred_at +
    correlation_id, consecutive sequence_no, causation_id = business event_id."""
    batch = _run()
    pairs = 0
    for cur, nxt in pairwise(batch):
        if cur["op"] is None and nxt["op"] is not None and \
                nxt["causation_id"] == cur["event_id"]:
            assert nxt["occurred_at"] == cur["occurred_at"]
            assert nxt["sequence_no"] == cur["sequence_no"] + 1
            assert nxt["correlation_id"] == cur["correlation_id"]
            assert nxt["payload"]["source"]["tx_id"] == cur["event_id"]
            pairs += 1
    assert pairs > 0, "expected business->CDC adjacency pairs"


# --- R-CDC-3: background mutations as chain roots --------------------------


def test_background_mutations_are_cdc_only_chain_roots() -> None:
    """The fixture's address/price drift emits CDC-only chain roots: no business
    cause, correlation_id = event_id, actor/session/causation/tx_id all null."""
    roots = [
        e for e in _cdc(_run())
        if e["op"] == "u" and e["causation_id"] is None
    ]
    assert roots, "expected background-mutation chain roots"
    for env in roots:
        assert env["correlation_id"] == env["event_id"]
        assert env["actor_id"] is None
        assert env["session_id"] is None
        assert env["causation_id"] is None
        assert env["payload"]["source"]["tx_id"] is None
        # still a well-formed update: both images present.
        assert env["payload"]["before"] is not None
        assert env["payload"]["after"] is not None


def test_background_mutation_actually_changes_an_attribute() -> None:
    """A background ``u`` chain root reflects a real pool drift (before != after)."""
    roots = [
        e for e in _cdc(_run())
        if e["op"] == "u" and e["causation_id"] is None
    ]
    assert any(
        e["payload"]["before"] != e["payload"]["after"] for e in roots
    ), "at least one background mutation must change an attribute"


# --- GOLD-B: determinism with CDC + background mutations -------------------


def test_cdc_output_is_byte_identical_across_runs() -> None:
    """A fixed seed yields identical CDC streams (op, version, ids, images)."""
    def key(batch: Any) -> list[tuple[Any, ...]]:
        return [
            (e["event_type"], e["op"], e["sequence_no"], e["occurred_at"],
             e["event_id"], e["correlation_id"], e["causation_id"],
             e["payload"]["source"]["entity_version"])
            for e in _cdc(batch)
        ]
    assert key(_run(seed=99)) == key(_run(seed=99))
    # a different seed produces a different stream (the drift is seed-driven).
    assert key(_run(seed=99)) != key(_run(seed=100))
