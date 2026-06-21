"""Pure stage tests for ``late_arriving`` (chaos-engine §5.7, event-model §3.4).

Framework-free (no Django, no DB): the stage selects instances, computes
``due_at = canonical emitted_at + simulated_delay / k``, records the injection
(``outcome: pending``) BEFORE extracting, and emits a :class:`ScheduledEntry` via
the late-buffer port. Covers: configured rate; determinism; the ``k`` clock
conversion (k=1 vs k=60); record-before-extract; extraction (selected instances
leave the in-line flow); the input batch is never mutated (CHD-4/5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from dataforge_engine.chaos import StageContext, chaos_subseed
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.chaos.stages.late_arriving import LateArrivingStage, ScheduledEntry

from .fixtures import SEED, SHARD_ID, STREAM_ID, WORKSPACE_ID, make_batch

N = 4000
# A fixed delay so due_at arithmetic is exact and assertion-friendly (PT30M).
_FIXED_DELAY: dict[str, Any] = {"family": "fixed", "value": "PT30M"}
_PARAMS_FIXED: dict[str, Any] = {
    "delay": _FIXED_DELAY,
    "max_delay": "PT24H",
    "event_types": ["*"],
}
_30M_MS = 30 * 60 * 1000


class _Collector:
    """An in-memory late-buffer port that records inserted :class:`ScheduledEntry`."""

    def __init__(self) -> None:
        self.entries: list[ScheduledEntry] = []

    def insert(self, entry: object) -> None:
        self.entries.append(entry)  # type: ignore[arg-type]


class _Clock:
    def __init__(self, k: float) -> None:
        self.speed_multiplier = k


def _ctx(rec: InMemoryRecorder, buf: _Collector, k: float = 1.0) -> StageContext:
    return StageContext(
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        workspace_id=WORKSPACE_ID,
        chaos_subseed=chaos_subseed(SEED),
        recorder=rec,
        late_buffer=buf,
        mode_config={"enabled": True, "rate": 0.03, "params": _PARAMS_FIXED},
        virtual_clock=_Clock(k),
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_selected_instances_leave_the_inline_flow() -> None:
    """Selected lates are EXTRACTED (not returned) and become buffer entries."""
    rec, buf = InMemoryRecorder(), _Collector()
    batch = make_batch(N)
    out = LateArrivingStage().process(batch, _ctx(rec, buf))
    assert len(buf.entries) > 0  # some selected at rate 0.03
    assert len(out) == N - len(buf.entries)  # extracted = removed in-line
    assert len(rec.records) == len(buf.entries)  # one record per extraction


def test_due_at_k1_equals_emitted_plus_simulated_delay() -> None:
    """At k=1 wall_delay == simulated_delay: due_at = emitted_at + 30 min (§3.4)."""
    rec, buf = InMemoryRecorder(), _Collector()
    LateArrivingStage().process(make_batch(N), _ctx(rec, buf, k=1.0))
    entry = buf.entries[0]
    canonical_emitted = _parse(entry["envelope"]["emitted_at"])
    due = _parse(entry["due_at"])
    assert (due - canonical_emitted).total_seconds() == 30 * 60
    assert entry["delay_simulated_ms"] == _30M_MS


def test_due_at_k60_is_thirty_wall_seconds() -> None:
    """At k=60, 30 simulated minutes realize as 30 WALL seconds (§3.4 worked row)."""
    rec, buf = InMemoryRecorder(), _Collector()
    LateArrivingStage().process(make_batch(N), _ctx(rec, buf, k=60.0))
    entry = buf.entries[0]
    canonical_emitted = _parse(entry["envelope"]["emitted_at"])
    due = _parse(entry["due_at"])
    assert (due - canonical_emitted).total_seconds() == 30.0  # 1800s / 60
    # The simulated delay recorded on the entry/record is unchanged by k.
    assert entry["delay_simulated_ms"] == _30M_MS


def test_record_outcome_pending_and_carries_due_at() -> None:
    """The injection is recorded ``pending`` with the simulated delay + due_at (§5.7)."""
    rec, buf = InMemoryRecorder(), _Collector()
    LateArrivingStage().process(make_batch(N), _ctx(rec, buf))
    record = rec.records[0]
    assert record["mode"] == "late_arriving"
    details = record["details"]
    assert details["outcome"] == "pending"
    assert details["delay_simulated_ms"] == _30M_MS
    assert "due_at_wall" in details
    assert details["duplicate_index"] == 0


def test_deterministic_same_seed_same_selection() -> None:
    """Same (seed, config, batch) ⇒ identical extractions + due_at (INV-CHA-2)."""
    out1: list[Any] = []
    out2: list[Any] = []
    for sink in (out1, out2):
        rec, buf = InMemoryRecorder(), _Collector()
        LateArrivingStage().process(make_batch(N), _ctx(rec, buf))
        sink.extend((e["event_id"], e["due_at"]) for e in buf.entries)
    assert out1 == out2


def test_disabled_is_identity() -> None:
    rec, buf = InMemoryRecorder(), _Collector()
    batch = make_batch(50)
    ctx = _ctx(rec, buf)
    ctx.mode_config = {"enabled": False, "rate": 0.03, "params": _PARAMS_FIXED}
    out = LateArrivingStage().process(batch, ctx)
    assert out == batch
    assert buf.entries == []


def test_input_batch_never_mutated() -> None:
    """The stored envelope is a clone; the input (ledger) batch stays canonical."""
    rec, buf = InMemoryRecorder(), _Collector()
    batch = make_batch(N)
    LateArrivingStage().process(batch, _ctx(rec, buf))
    # Every input envelope is still canonical (no _df.chaos.late_arriving stamped).
    for env in batch:
        chaos = env["_df"].get("chaos")
        assert chaos is None or "late_arriving" not in chaos
    # The stored (extracted) envelope IS labelled.
    stored_chaos = buf.entries[0]["envelope"]["_df"]["chaos"]
    assert stored_chaos is not None
    assert stored_chaos["late_arriving"]
