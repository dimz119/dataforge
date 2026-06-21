"""``corrupted_values`` — type-breaking mutations (chaos-engine §5.3, O-3).

Third stage, before ``nulls``. On selection (``draw(corrupted_values, event_id,
"select") < rate``): choose ``max_fields_per_event`` distinct eligible payload
leaves via ``draw(…, "field:n")``, then a valid kind per field via
``draw(…, "kind:n")``; apply the closed-vocabulary mutation; label
``_df.chaos.corrupted_values = {mutations:[{path, original_value}]}``.

PAYLOAD-only — scalar leaves of the business ``payload`` (or CDC ``before``/
``after`` images), NEVER envelope fields (CR-6) and never the Debezium frame.
Keyed per canonical event (CR-1): every duplicate copy already carries the
identical corruption because copies are produced upstream as byte-identical
clones and re-derive the same decision. The record is written BEFORE the
corrupted instance is published (INV-CHA-4).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataforge_engine.envelope import InternalEnvelope
from dataforge_engine.envelope.types import JSONValue

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u, weighted_choice
from ..record import InjectionRecord, deterministic_injection_id
from ._common import clone_envelope, iter_payload_leaves, label_touched, set_payload_leaf
from .corruption_vocab import apply_kind, infer_value_type, valid_kinds_for

MODE: ChaosMode = "corrupted_values"


def _max_fields(params: dict[str, object]) -> int:
    raw = params.get("max_fields_per_event", 1)
    return int(raw) if isinstance(raw, int) else 1


class CorruptedValuesStage:
    """The ``corrupted_values`` mode stage (§5.3)."""

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
            mutated = self._corrupt(envelope, ctx, max_fields)
            out.append(mutated if mutated is not None else envelope)
        return out

    def _corrupt(
        self, envelope: InternalEnvelope, ctx: StageContext, max_fields: int
    ) -> InternalEnvelope | None:
        event_id = envelope["event_id"]
        # Eligible scalar leaves with at least one valid kind, in deterministic order.
        leaves = [
            (path, value)
            for path, value in iter_payload_leaves(envelope["payload"])
            if valid_kinds_for(infer_value_type(value))
        ]
        if not leaves:
            return None
        count = min(max_fields, len(leaves))
        clone = clone_envelope(envelope)
        mutations: list[JSONValue] = []
        chosen: set[int] = set()
        for n in range(count):
            index = self._pick_field(ctx, event_id, n, len(leaves), chosen)
            chosen.add(index)
            path, original = leaves[index]
            kinds = valid_kinds_for(infer_value_type(original))
            kind_u = draw_u(ctx.chaos_subseed, MODE, event_id, f"kind:{n}")
            kind = kinds[weighted_choice(kind_u, [1.0] * len(kinds))]
            set_payload_leaf(clone["payload"], path, apply_kind(kind, original))
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
        ctx.recorder.record(record)  # BEFORE publishing the mutated instance
        label_touched(clone, injection_id, MODE, {"mutations": mutations})
        return clone

    @staticmethod
    def _pick_field(ctx: StageContext, event_id: str, n: int, total: int, used: set[int]) -> int:
        """A distinct eligible-field index via ``draw(…, "field:n")`` (uniform)."""
        index = int(draw_u(ctx.chaos_subseed, MODE, event_id, f"field:{n}") * total)
        if index >= total:
            index = total - 1
        # Linear-probe to the next free index for distinctness (deterministic).
        while index in used:
            index = (index + 1) % total
        return index
