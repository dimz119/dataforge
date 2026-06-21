"""``nulls`` — unexpected nulls on payload targets (chaos-engine §5.4, O-3).

Fourth stage, after ``corrupted_values``. On selection (``draw(nulls, event_id,
"select") < rate``): choose target leaves via ``draw(…, "field:n")``; set each to
JSON ``null`` (the KEY remains present — a null, not a missing field); label
``_df.chaos.nulls = {mutations:[{path, original_value}]}``.

PAYLOAD-only — scalar leaves of ``payload`` only, NEVER envelope fields (CR-6).
Fields already mutated by ``corrupted_values`` on the same event are EXCLUDED
(CR-4, disjoint-field rule) — read off the upstream ``_df.chaos.corrupted_values``
labels so every mutated field maps to exactly one injection record. Keyed per
canonical event (CR-1). The record is written BEFORE publishing (INV-CHA-4).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import cast

from dataforge_engine.envelope import InternalEnvelope
from dataforge_engine.envelope.types import JSONValue

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u
from ..record import InjectionRecord, deterministic_injection_id
from ._common import clone_envelope, iter_payload_leaves, label_touched, set_payload_leaf

MODE: ChaosMode = "nulls"


def _max_fields(params: dict[str, object]) -> int:
    raw = params.get("max_fields_per_event", 1)
    return int(raw) if isinstance(raw, int) else 1


def _already_corrupted_paths(envelope: InternalEnvelope) -> set[str]:
    """Paths corrupted upstream this event (CR-4 disjoint-field exclusion)."""
    chaos = envelope["_df"].get("chaos")
    if not chaos or "corrupted_values" not in chaos:
        return set()
    mutations = cast(dict[str, object], chaos["corrupted_values"]).get("mutations", [])
    if not isinstance(mutations, list):
        return set()
    return {cast(str, m["path"]) for m in mutations if isinstance(m, dict) and "path" in m}


class NullsStage:
    """The ``nulls`` mode stage (§5.4)."""

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
        max_fields = _max_fields(params)
        out: list[InternalEnvelope] = []
        for envelope in batch:
            if not event_type_eligible(envelope["event_type"], selector):
                out.append(envelope)
                continue
            event_id = envelope["event_id"]
            if draw_u(ctx.chaos_subseed, MODE, event_id, "select") >= rate:
                out.append(envelope)
                continue
            mutated = self._nullify(envelope, ctx, max_fields)
            out.append(mutated if mutated is not None else envelope)
        return out

    def _nullify(
        self, envelope: InternalEnvelope, ctx: StageContext, max_fields: int
    ) -> InternalEnvelope | None:
        event_id = envelope["event_id"]
        excluded = _already_corrupted_paths(envelope)
        leaves = [
            (path, value)
            for path, value in iter_payload_leaves(envelope["payload"])
            if path not in excluded and value is not None
        ]
        if not leaves:
            return None
        count = min(max_fields, len(leaves))
        clone = clone_envelope(envelope)
        mutations: list[JSONValue] = []
        chosen: set[int] = set()
        total = len(leaves)
        for n in range(count):
            index = int(draw_u(ctx.chaos_subseed, MODE, event_id, f"field:{n}") * total)
            if index >= total:
                index = total - 1
            while index in chosen:
                index = (index + 1) % total
            chosen.add(index)
            path, original = leaves[index]
            set_payload_leaf(clone["payload"], path, cast(JSONValue, None))
            mutations.append({"path": path, "original_value": original})
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
            "details": {"mutations": mutations},
        }
        ctx.recorder.record(record)  # BEFORE publishing
        label_touched(clone, injection_id, MODE, {"mutations": mutations})
        return clone
