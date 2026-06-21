"""``out_of_order`` — tumbling-window shuffle (chaos-engine §5.6).

Sixth stage (after ``schema_drift``, before ``late_arriving``). Windows are
tumbling, per shard, in simulated time, anchored at the virtual epoch: window *i*
covers ``[epoch + i·W, epoch + (i+1)·W)`` and an instance belongs to the window
containing its ``occurred_at`` — membership is deterministic and arrival-
independent (the same events fall in the same windows on every replay).

At window close the eligible instances selected by ``draw(out_of_order,
(event_id, duplicate_index), "select") < rate`` (per instance, CR-2) have their
positions permuted by a seeded Fisher-Yates over those positions, RNG seeded by
``HMAC(chaos_subseed, "out_of_order:window:{shard}:{window_index}")``. The window
flushes in the permuted order. Every instance whose position CHANGED is labelled
``_df.chaos.out_of_order = {displaced_from_position}`` and gets one InjectionRecord
recording ``displaced_from_position`` + ``window_simulated_ms`` (a selected
instance the permutation maps to its own position is NOT an injection — §5.6.5).

This pure stage realises the deterministic window membership + permutation; the
runner supplies the virtual epoch via the ``virtual_clock`` port (a
:class:`VirtualClock`) and the wall hold/flush timing (§2.4). ``emitted_at`` is
stamped at publish by the runner, never here (INV-CHA-6). Deterministic from the
chaos sub-seed (INV-CHA-2).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dataforge_engine.envelope import InternalEnvelope
from dataforge_engine.envelope.types import JSONValue

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u
from ..record import InjectionRecord, deterministic_injection_id
from ._common import label_touched
from .iso_duration import parse_iso_duration_ms

MODE: ChaosMode = "out_of_order"

_DEFAULT_WINDOW = "PT60S"


@runtime_checkable
class VirtualClock(Protocol):
    """The ``virtual_clock`` port (§5.6): the shard's simulated virtual epoch."""

    @property
    def virtual_epoch_ms(self) -> int:
        """Epoch ms the tumbling windows anchor at (§5.6 window semantics)."""
        ...


def _occurred_ms(occurred_at: str) -> int:
    parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _instance_index(envelope: InternalEnvelope) -> int | None:
    """The instance's ``duplicate_index`` (CR-2 keying); ``None`` on the original."""
    chaos = envelope["_df"].get("chaos")
    if chaos and "duplicates" in chaos:
        idx = chaos["duplicates"].get("duplicate_index")
        if isinstance(idx, int):
            return idx
    return None


def _fisher_yates(positions: list[int], seed: bytes) -> list[int]:
    """Seeded Fisher-Yates permutation of ``positions`` (deterministic over seed).

    Draws are taken from successive HMAC blocks of ``seed`` so the permutation is a
    pure function of ``(shard_id, window_index, chaos_subseed)``.
    """
    out = list(positions)
    for i in range(len(out) - 1, 0, -1):
        block = hmac.new(seed, i.to_bytes(4, "big"), hashlib.sha256).digest()
        j = int.from_bytes(block[:8], "big") % (i + 1)
        out[i], out[j] = out[j], out[i]
    return out


class OutOfOrderStage:
    """The ``out_of_order`` mode stage (§5.6)."""

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
        window_ms = parse_iso_duration_ms(params.get("window", _DEFAULT_WINDOW))
        if window_ms <= 0:
            return batch
        epoch_ms = self._epoch_ms(ctx, batch)
        # Stable canonical order is the incoming order (ascending sequence_no,
        # copies after originals). Group positions into tumbling windows.
        windows: dict[int, list[int]] = {}
        for pos, envelope in enumerate(batch):
            w = (_occurred_ms(envelope["occurred_at"]) - epoch_ms) // window_ms
            windows.setdefault(w, []).append(pos)
        out: list[InternalEnvelope] = list(batch)
        for window_index, positions in windows.items():
            self._shuffle_window(
                out, batch, ctx, rate, selector, window_index, window_ms, positions
            )
        return out

    def _shuffle_window(
        self,
        out: list[InternalEnvelope],
        batch: list[InternalEnvelope],
        ctx: StageContext,
        rate: float,
        selector: object,
        window_index: int,
        window_ms: int,
        positions: list[int],
    ) -> None:
        # Local 0-based positions within the window, in canonical order.
        local = list(range(len(positions)))
        selected = [
            i
            for i, pos in enumerate(positions)
            if event_type_eligible(batch[pos]["event_type"], selector)
            and self._select(ctx, batch[pos], rate)
        ]
        if not selected:
            return
        permuted_sel = _fisher_yates(selected, self._window_seed(ctx, window_index))
        # Map selected local positions to their permuted destinations.
        dest = dict(zip(selected, permuted_sel, strict=True))
        new_local = list(local)
        for src, to in dest.items():
            new_local[to] = local[src]
        for local_dest, local_src in enumerate(new_local):
            if local_dest == local_src:
                continue  # position unchanged ⇒ not an injection (§5.6.5)
            global_dest = positions[local_dest]
            moved = batch[positions[local_src]]
            out[global_dest] = self._label(moved, ctx, local_src, window_ms)

    @staticmethod
    def _select(ctx: StageContext, envelope: InternalEnvelope, rate: float) -> bool:
        instance = _instance_index(envelope)
        return draw_u(
            ctx.chaos_subseed, MODE, envelope["event_id"], "select", instance
        ) < rate

    @staticmethod
    def _window_seed(ctx: StageContext, window_index: int) -> bytes:
        msg = f"{MODE}:window:{ctx.shard_id}:{window_index}".encode()
        return hmac.new(ctx.chaos_subseed, msg, hashlib.sha256).digest()

    @staticmethod
    def _label(
        envelope: InternalEnvelope, ctx: StageContext, from_position: int, window_ms: int
    ) -> InternalEnvelope:
        clone = cast(InternalEnvelope, copy.deepcopy(dict(envelope)))
        instance = _instance_index(envelope)
        detail: dict[str, JSONValue] = {
            "displaced_from_position": from_position,
            "window_simulated_ms": window_ms,
        }
        if instance is not None:
            detail["duplicate_index"] = instance
        injection_id = deterministic_injection_id(
            ctx.chaos_subseed, MODE, envelope["event_id"], envelope["occurred_at"], instance
        )
        record: InjectionRecord = {
            "injection_id": injection_id,
            "workspace_id": ctx.workspace_id,
            "stream_id": ctx.stream_id,
            "shard_id": ctx.shard_id,
            "mode": MODE,
            "event_id": envelope["event_id"],
            "sequence_no": envelope["sequence_no"],
            "occurred_at": envelope["occurred_at"],
            "canonical_emitted_at": envelope["emitted_at"],
            "details": cast("dict[str, object]", detail),
        }
        ctx.recorder.record(record)  # BEFORE the reordered flush (INV-CHA-4)
        label_touched(clone, injection_id, MODE, detail)
        return clone

    @staticmethod
    def _epoch_ms(ctx: StageContext, batch: list[InternalEnvelope]) -> int:
        clock = ctx.virtual_clock
        if isinstance(clock, VirtualClock):
            return clock.virtual_epoch_ms
        # No clock wired: anchor at the batch's earliest occurred_at (deterministic,
        # arrival-independent for a given batch).
        return min(_occurred_ms(e["occurred_at"]) for e in batch) if batch else 0
