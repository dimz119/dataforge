"""Checkpoint codec + restore (behavior-engine §9.1, §9.3).

One checkpoint blob per (stream, shard): versioned canonical JSON capturing actor
*traversal* states, pending timers, PRNG cursor positions, the arrival integrator,
the TPS schedule tail, the sequence counter, and per-type pool counters. Pool
**contents** are deliberately absent — they live in ``entity_pool_snapshots`` rows
written in the same cycle under the commit-marker rule (§9.1). This FORMAT ships
now for batch finalization; lease-driven pause/resume is Phase 5-6.

The codec emits/consumes a JSON-serializable ``dict`` (the host zstd-compresses
and persists it). Restore rebuilds the heap, cursors, arrival position, sequence,
and traversals from the blob; the host loads pool images separately (§9.3 step 2).

Pure Python; ``json`` (stdlib) only (BE-ENG-1).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .errors import CheckpointError
from .rng import traversal_rng
from .runtime import Traversal
from .scheduler import ArrivalState, Timer

if TYPE_CHECKING:
    from .shard import Shard

CODEC_VERSION = 1
CODEC_MINOR = 0


def encode_checkpoint(
    shard: Shard, *, checkpoint_seq: int, config_sha256: str = ""
) -> dict[str, Any]:
    """Serialize a shard's resumable state into the §9.1 checkpoint blob."""
    config = shard.config
    ir = shard.ir
    sessions: dict[str, Any] = {}
    lifecycles: dict[str, Any] = {}
    for tid, traversal in shard.interpreter.traversals.items():
        record = _encode_traversal(traversal)
        if traversal.kind == "session":
            sessions[tid] = record
        else:
            lifecycles[tid] = record

    pool_counters: dict[str, Any] = {}
    for name in ir.entity_order:
        pool = shard.pools.pool(name)
        pool_counters[name] = {
            "key_counter": pool.key_counter,
            "created_total": pool.created_total,
            "archived_total": pool.archived_total,
        }

    return {
        "codec_version": CODEC_VERSION,
        "codec_minor": CODEC_MINOR,
        "pin_echo": {
            "scenario_slug": ir.slug,
            "manifest_version": ir.version,
            "config_sha256": config_sha256,
            "seed": config.seed,
            "shard_count": config.shard_count,
        },
        "checkpoint_seq": checkpoint_seq,
        "sequence_no_last": shard.sequence.last,
        "vclock": {
            "virtual_epoch_ms": shard.clock.virtual_epoch_ms,
            "speed_multiplier": shard.clock.speed_multiplier,
            "frontier_us": shard.clock.frontier_us,
            "mode": shard.clock.mode,
        },
        "arrival": {
            "next_index": shard.arrival.state.next_index,
            "solve_from_us": shard.arrival.state.solve_from_us,
            "gap_remaining": shard.arrival.state.gap_remaining,
        },
        "timer_seq_next": shard.heap.timer_seq_next,
        "timers": [_encode_timer(t) for t in shard.heap.entries()],
        "sessions": sessions,
        "lifecycles": lifecycles,
        "pool_counters": pool_counters,
        "bg_day_cursor": -1,  # Phase 8 (background mutations); reserved here.
    }


def _encode_traversal(traversal: Traversal) -> dict[str, Any]:
    return {
        "machine": traversal.machine,
        "machine_state": traversal.state,
        "actor_key": traversal.actor_key,
        "subject_type": traversal.subject_type,
        "subject_key": traversal.subject_key,
        "memory": traversal.memory,
        "rng_cursors": {
            "transitions": traversal.rng.transitions.position,
            "values": traversal.rng.values.position,
        },
        "correlation_id": traversal.correlation_id,
        "last_event_id": traversal.last_event_id,
        "spawned_at_us": traversal.spawned_at_us,
        "transition_count": traversal.transition_count,
        "session_id": traversal.session_id,
        "pending_transition_idx": traversal.pending_transition_idx,
    }


def _encode_timer(timer: Timer) -> dict[str, Any]:
    return {
        "virtual_due_at": timer.virtual_due_at,
        "timer_seq": timer.timer_seq,
        "kind": timer.kind,
        "ref": timer.ref,
    }


def encode_to_json(shard: Shard, *, checkpoint_seq: int, config_sha256: str = "") -> str:
    """Canonical JSON string for the host to zstd-compress (≤ 32 MiB; §9.1)."""
    blob = encode_checkpoint(shard, checkpoint_seq=checkpoint_seq, config_sha256=config_sha256)
    return json.dumps(blob, separators=(",", ":"), sort_keys=True)


def restore_checkpoint(shard: Shard, blob: dict[str, Any]) -> None:
    """Rebuild a shard's resumable state from a checkpoint blob (§9.3 steps 3-4).

    Pool images must already be loaded into ``shard.pools`` by the host (§9.3 step
    2); this restores the heap, cursors, arrival position, sequence, clock
    frontier, and traversals. Raises :class:`CheckpointError` on a ``pin_echo``
    mismatch or an unknown ``codec_version``.
    """
    if int(blob.get("codec_version", -1)) != CODEC_VERSION:
        raise CheckpointError(f"unknown codec_version {blob.get('codec_version')!r} (§9.1)")
    _verify_pin(shard, blob.get("pin_echo", {}))

    shard.ensure_registered()
    shard.mark_seeded()  # restore never re-seeds (§9.3)
    shard.restore_sequence(int(blob["sequence_no_last"]))
    vclock = blob["vclock"]
    shard.clock.frontier_us = int(vclock["frontier_us"])
    shard.clock.mode = str(vclock["mode"])

    arr = blob["arrival"]
    shard.set_arrival_state(ArrivalState(
        next_index=int(arr["next_index"]),
        solve_from_us=int(arr["solve_from_us"]),
        gap_remaining=float(arr["gap_remaining"]),
    ))

    # Rebuild traversals (sessions + lifecycles) with restored cursor positions.
    for tid, rec in {**blob.get("sessions", {}), **blob.get("lifecycles", {})}.items():
        _restore_traversal(shard, tid, rec)

    # Rebuild the heap from serialized entries (preserving timer_seq, §3.2).
    for entry in blob.get("timers", []):
        shard.heap.push_existing(Timer(
            virtual_due_at=int(entry["virtual_due_at"]),
            timer_seq=int(entry["timer_seq"]),
            kind=entry["kind"],
            ref=dict(entry["ref"]),
        ))

    # Restore per-type pool counters (derived indexes were rebuilt on image load).
    for name, counters in blob.get("pool_counters", {}).items():
        pool = shard.pools.pool(name)
        pool.key_counter = int(counters["key_counter"])
        pool.created_total = int(counters["created_total"])
        pool.archived_total = int(counters["archived_total"])


def _restore_traversal(shard: Shard, tid: str, rec: dict[str, Any]) -> None:
    machine = rec["machine"]
    is_session = rec.get("session_id") is not None
    ctx_key = f"session:{tid}" if is_session else f"lifecycle:{tid}"
    cursors = rec["rng_cursors"]
    rng = traversal_rng(
        shard.tree, transitions_ctx=ctx_key, values_ctx=ctx_key,
        transitions_pos=int(cursors["transitions"]),
        values_pos=int(cursors["values"]),
    )
    traversal = Traversal(
        traversal_id=tid, machine=machine, kind="session" if is_session else "lifecycle",
        state=rec["machine_state"], actor_key=rec.get("actor_key"),
        subject_type=rec.get("subject_type"), subject_key=rec.get("subject_key"),
        rng=rng, correlation_id=rec.get("correlation_id", ""),
        last_event_id=rec.get("last_event_id"), memory=dict(rec.get("memory", {})),
        spawned_at_us=int(rec.get("spawned_at_us", 0)),
        transition_count=int(rec.get("transition_count", 0)),
        session_id=rec.get("session_id"),
        pending_transition_idx=rec.get("pending_transition_idx"),
    )
    shard.interpreter.traversals[tid] = traversal


def _verify_pin(shard: Shard, pin: dict[str, Any]) -> None:
    config = shard.config
    ir = shard.ir
    expected: dict[str, Any] = {
        "scenario_slug": ir.slug,
        "manifest_version": ir.version,
        "seed": config.seed,
        "shard_count": config.shard_count,
    }
    for field_name, value in expected.items():
        if pin.get(field_name) != value:
            raise CheckpointError(
                f"checkpoint pin_echo.{field_name}={pin.get(field_name)!r} != "
                f"stream pin {value!r} (corruption; refuse to start, T4)"
            )
