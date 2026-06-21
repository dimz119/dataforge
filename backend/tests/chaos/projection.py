"""The deterministic chaos projection — the shared no-broker harness (§11, CHD).

Runs the PURE :class:`ChaosPipeline` over a generated canonical batch with an
in-memory recorder + late-buffer collector, returning everything the statistical
(STAT-C), determinism (CHD-1/2/3), and reconciliation (CHD-4/5) suites need:

* ``ledger`` — the canonical input batch (the immutable ground truth);
* ``delivered`` — the post-chaos in-line stream (everything published this tick,
  EXCLUDING late-extracted instances — those leave the in-line flow, §5.7/O-6);
* ``records`` — the :class:`InjectionRecord` answer-key rows (one per injection);
* ``late_entries`` — the extracted ``ScheduledEntry`` descriptors (the buffer).

No Django, no broker, no DB — pure engine + ports, so it rides the fast lane.
The :func:`deterministic_projection` mapping drops wall-clock artifacts
(``injection_id``, ``recorded_at``, ``due_at``) so two runs compare on the chaos
DECISIONS only (CHD-1). :func:`ledger_content_hash` proves CHD-5 immutability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast

from dataforge_engine.chaos import (
    ChaosPipeline,
    ChaosPolicy,
    InjectionRecord,
    StageContext,
    chaos_subseed,
    default_policy,
)
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.chaos.tests.fixtures import (
    SHARD_ID,
    STREAM_ID,
    WORKSPACE_ID,
    FakeDriftMenu,
    FakeRegistryView,
    FakeVirtualClock,
    base_epoch_ms,
    make_batch,
)
from dataforge_engine.envelope import InternalEnvelope, canonical_serialize

# The v2 drift menu the projection arms (matches the §5.5 worked example): every
# subject gains an optional ``shipping_state`` string at v2.
_V2_FIELD = {"path": "shipping_state", "fragment": {"type": "string"}}


class _LateCollector:
    """An in-memory ``LateBuffer`` port capturing extracted ``ScheduledEntry``s."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def insert(self, entry: object) -> None:
        self.entries.append(dict(cast("dict[str, Any]", entry)))


@dataclass
class Projection:
    """One deterministic chaos run's full ground truth (no wall clock, no broker)."""

    ledger: list[InternalEnvelope]
    delivered: list[InternalEnvelope]
    records: list[InjectionRecord]
    late_entries: list[dict[str, Any]] = field(default_factory=list)

    def records_for(self, mode: str) -> list[InjectionRecord]:
        return [r for r in self.records if r["mode"] == mode]

    def delivered_dicts(self) -> list[dict[str, Any]]:
        """Delivered instances as plain dicts (dynamic-key access for reconciliation)."""
        return [dict(e) for e in self.delivered]

    def all_instance_dicts(self) -> list[dict[str, Any]]:
        """Delivered in-line + late-extracted instances as plain dicts (full truth).

        Late extractions leave the in-line flow (§5.7) but still carry their
        content-mode ``_df`` blocks, so value-mode / duplicate reconciliation must
        span both surfaces (CR-1/CR-2)."""
        late = [dict(entry["envelope"]) for entry in self.late_entries]
        return [*self.delivered_dicts(), *late]


def _drift_menu(event_types: list[str]) -> FakeRegistryView:
    """A registry view giving every fixture subject a next (v2) version."""
    subjects = {f"shop.{t}": FakeDriftMenu(1, 2, [dict(_V2_FIELD)]) for t in event_types}
    return FakeRegistryView(subjects)


def run_projection(
    policy: ChaosPolicy,
    *,
    n: int = 5000,
    event_type: str = "order_placed",
    k: float = 1.0,
) -> Projection:
    """Apply ``policy`` to ``n`` fresh canonical envelopes via the pure pipeline.

    ``schema_drift`` is armed with a v2 menu for the batch's event type and
    ``out_of_order``/``late_arriving`` get a virtual clock (anchored at the
    fixture epoch, ``speed_multiplier = k``). The same ``(seed, policy, n)``
    always produces the same projection (INV-CHA-2).
    """
    ledger = make_batch(n, event_type=event_type)
    canonical_input = make_batch(n, event_type=event_type)  # independent reference
    recorder = InMemoryRecorder()
    late = _LateCollector()
    clock = FakeVirtualClock(base_epoch_ms())
    clock.speed_multiplier = k  # type: ignore[attr-defined]
    ctx = StageContext(
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        workspace_id=WORKSPACE_ID,
        chaos_subseed=chaos_subseed(424242),
        recorder=recorder,
        late_buffer=late,
        registry_view=_drift_menu([event_type]),
        virtual_clock=clock,
    )
    delivered = ChaosPipeline(policy).transform(ledger, ctx)
    return Projection(
        ledger=canonical_input,
        delivered=delivered,
        records=recorder.records,
        late_entries=late.entries,
    )


def all_modes_policy(rate: float) -> ChaosPolicy:
    """A policy with all seven modes enabled at ``rate`` (the GOLD-C / CHD-7 base)."""
    policy = default_policy()
    for mode in (
        "missing",
        "duplicates",
        "corrupted_values",
        "nulls",
        "schema_drift",
        "out_of_order",
        "late_arriving",
    ):
        policy[mode]["enabled"] = True
        policy[mode]["rate"] = rate
    # A fixed late delay keeps due_at arithmetic exact for byte-identity (GOLD-C).
    policy["late_arriving"]["params"]["delay"] = {"family": "fixed", "value": "PT30M"}
    return policy


def _projection_row(record: InjectionRecord) -> tuple[Any, ...]:
    """The CHD-1 deterministic projection of one record (wall-clock fields dropped).

    Keeps ``(mode, event_id, sequence_no, duplicate_index, sorted detail items)``
    minus ``injection_id``/``recorded_at``/``due_at_wall``/realized-wall fields —
    those are wall-clock artifacts, not chaos decisions (§11, CHD-1).
    """
    details = {
        key: value
        for key, value in record["details"].items()
        if key not in ("due_at_wall", "realized_wall_delay_ms", "outcome")
    }
    # Content-keyed modes carry no duplicate_index; normalise to -1 so the
    # projection rows are totally ordered (None vs int is not comparable).
    dup = record["details"].get("duplicate_index")
    dup_sort = int(dup) if isinstance(dup, int) else -1
    return (
        record["mode"],
        record["event_id"],
        record["sequence_no"],
        dup_sort,
        json.dumps(details, sort_keys=True, default=str),
    )


def deterministic_projection(records: list[InjectionRecord]) -> list[tuple[Any, ...]]:
    """The sorted CHD-1 projection set — equal across identical (seed, config) runs."""
    return sorted(_projection_row(r) for r in records)


def ledger_content_hash(ledger: list[InternalEnvelope]) -> str:
    """SHA-256 over the canonically-serialized ledger (CHD-5 immutability proof)."""
    digest = hashlib.sha256()
    for envelope in ledger:
        digest.update(canonical_serialize(envelope))
        digest.update(b"\n")
    return digest.hexdigest()
