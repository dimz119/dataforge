"""``missing`` — suppression with the ledger retained (chaos-engine §5.1, O-1).

First stage. For each eligible event, if ``draw(missing, event_id, "select") <
rate`` the event is REMOVED from the batch — never published on any instance. The
ledger row is untouched (committed before the stage ran); the answer key lists
every suppressed ``event_id`` with its full canonical position. Keyed per
canonical event (CR-1): a suppressed event also produces no duplicates, no
mutations, no late entry — it simply never existed in delivery truth.

The InjectionRecord is written BEFORE the event is dropped (INV-CHA-4). There is
no delivered instance, so no ``_df.chaos`` shape exists for this mode.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataforge_engine.envelope import InternalEnvelope

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u
from ..record import InjectionRecord, deterministic_injection_id

MODE: ChaosMode = "missing"


class MissingStage:
    """The ``missing`` mode stage (§5.1)."""

    mode = MODE

    def process(
        self, batch: list[InternalEnvelope], ctx: StageContext
    ) -> list[InternalEnvelope]:
        config = ctx.mode_config
        if config is None or not config["enabled"]:
            return batch
        rate = config["rate"]
        selector = config["params"].get("event_types", ["*"])
        kept: list[InternalEnvelope] = []
        for envelope in batch:
            if not event_type_eligible(envelope["event_type"], selector):
                kept.append(envelope)
                continue
            event_id = envelope["event_id"]
            if draw_u(ctx.chaos_subseed, MODE, event_id, "select") < rate:
                # Record BEFORE suppression (INV-CHA-4); then drop the event.
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
                    "details": {},
                }
                ctx.recorder.record(record)
                continue  # suppressed — not appended
            kept.append(envelope)
        return kept
