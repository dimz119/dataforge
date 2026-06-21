"""``duplicates`` — byte-identical re-deliveries (chaos-engine §5.2, O-2).

Second stage, before the value stages so the value stages stamp identical
mutations onto every copy (that is what makes duplicate pairs byte-identical —
event-model §7.3). Per canonical event (CR-1):

1. select: ``draw(duplicates, event_id, "select") < rate``;
2. copy count: weighted choice over ``params.copies`` via ``draw(…, "copies")``;
3. each copy is a byte-identical clone of the original's delivered envelope —
   same ``event_id``, ``sequence_no``, ``occurred_at``, AND ``emitted_at``; only
   ``_df`` differs: ``_df.chaos.duplicates.duplicate_index = i`` (original 0,
   copies ≥ 1), ``_df.canonical = false`` on copies.

This Phase-9-modes-1-4 implementation realises ``adjacent`` spacing (copies
emitted immediately after the original). ``gap`` spacing rides the same selection
and is reserved to the buffer-aware stage work (the deterministic hold buffer);
``adjacent`` covers presets E1 (Dedup 101). The record is written BEFORE the
copies are published (INV-CHA-4).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import copy
from typing import cast

from dataforge_engine.envelope import InternalEnvelope

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u, weighted_choice
from ..record import InjectionRecord, deterministic_injection_id
from ._common import label_touched

MODE: ChaosMode = "duplicates"


def _copy_weights(params: dict[str, object]) -> tuple[list[int], list[float]]:
    """Parse ``params.copies`` → (counts, weights). Default ``[{count:1,weight:1.0}]``."""
    entries = params.get("copies")
    if not isinstance(entries, list) or not entries:
        return [1], [1.0]
    counts: list[int] = []
    weights: list[float] = []
    for entry in entries:
        if isinstance(entry, dict):
            counts.append(int(cast(int, entry.get("count", 1))))
            weights.append(float(cast(float, entry.get("weight", 1.0))))
    if not counts:
        return [1], [1.0]
    return counts, weights


class DuplicatesStage:
    """The ``duplicates`` mode stage (§5.2)."""

    mode = MODE

    def process(
        self, batch: list[InternalEnvelope], ctx: StageContext
    ) -> list[InternalEnvelope]:
        config = ctx.mode_config
        if config is None or not config["enabled"]:
            return batch
        rate = config["rate"]
        params = config["params"]
        selector = params.get("event_types", ["*"])
        counts, weights = _copy_weights(params)
        out: list[InternalEnvelope] = []
        for envelope in batch:
            out.append(envelope)  # the original (duplicate_index 0)
            if not event_type_eligible(envelope["event_type"], selector):
                continue
            event_id = envelope["event_id"]
            if draw_u(ctx.chaos_subseed, MODE, event_id, "select") >= rate:
                continue
            copies_u = draw_u(ctx.chaos_subseed, MODE, event_id, "copies")
            n_copies = counts[weighted_choice(copies_u, weights)]
            if n_copies <= 0:
                continue
            injection_id = deterministic_injection_id(
                ctx.chaos_subseed, MODE, event_id, envelope["occurred_at"]
            )
            record: InjectionRecord = {
                "injection_id": injection_id,
                "workspace_id": ctx.workspace_id,
                "stream_id": ctx.stream_id,
                "shard_id": ctx.shard_id,
                "mode": MODE,
                "event_id": event_id,
                "sequence_no": envelope["sequence_no"],
                "occurred_at": envelope["occurred_at"],
                "canonical_emitted_at": envelope["emitted_at"],
                "details": {"copies": n_copies},
            }
            ctx.recorder.record(record)  # BEFORE publishing the copies (INV-CHA-4)
            for index in range(1, n_copies + 1):
                clone = cast(InternalEnvelope, copy.deepcopy(dict(envelope)))
                label_touched(clone, injection_id, MODE, {"duplicate_index": index})
                out.append(clone)  # adjacent spacing
        return out
