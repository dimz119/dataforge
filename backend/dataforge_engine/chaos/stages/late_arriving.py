"""``late_arriving`` — simulated-time delay via the durable buffer (§5.7, O-6).

Terminal stage (§2.2): a late selection physically LEAVES the in-line flow into
the durable late-arrival buffer and re-enters at the publish boundary at ``due_at``
(§6). Per instance (CR-2, keyed on ``(event_id, duplicate_index)``):

1. select: ``draw(late_arriving, event_id, "select", dup) < rate``;
2. delay: a draw from ``params.delay`` (a §9.1 distribution in SIMULATED time),
   clamped to ``params.max_delay``; ``draw(…, "delay", dup)``;
3. ``wall_delay = simulated_delay / k`` and ``due_at = canonical emitted_at +
   wall_delay`` (event-model §3.4 — the only clock conversion in chaos);
4. the selected instance is EXTRACTED (not returned this tick); the stage emits a
   :class:`ScheduledEntry` descriptor (§6.1 shape) via ``ctx.late_buffer.insert``
   carrying the full internal envelope (with ``_df.chaos.late_arriving``) and
   records the injection (``outcome: pending``) BEFORE the extraction (INV-CHA-4).

Unselected instances pass through untouched. Pure/deterministic from the chaos
sub-seed: the same ``(seed, config, batch)`` selects the same instances with the
same delays (INV-CHA-2). Wall time enters only as ``due_at`` arithmetic from the
canonical ``emitted_at`` already on the envelope — no wall-clock read here.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypedDict, cast

from dataforge_engine.behavior.distributions import compile_dwell
from dataforge_engine.envelope import InternalEnvelope

from ..context import StageContext
from ..policy import ChaosMode, event_type_eligible
from ..prf import draw_u
from ..record import InjectionRecord, deterministic_injection_id
from ._common import clone_envelope, label_touched

MODE: ChaosMode = "late_arriving"

# Default delay distribution + clamp (§3.2 preset / §5.7 defaults).
_DEFAULT_DELAY: dict[str, object] = {
    "family": "lognormal",
    "median": "PT30M",
    "p95": "PT2H",
}
_DEFAULT_MAX_DELAY = "PT24H"
_US_PER_MS = 1000


class ScheduledEntry(TypedDict):
    """One pending re-emission descriptor (chaos-engine §6.1; the buffer host
    persists it to ``late_arrival_buffer``). ``envelope`` is the full INTERNAL
    envelope (incl. ``_df.chaos.late_arriving`` labels) — self-contained, so
    re-emission never reads the ledger on the hot path.
    """

    workspace_id: str
    stream_id: str
    shard_id: int
    injection_id: str
    event_id: str
    envelope: InternalEnvelope
    due_at: str
    delay_simulated_ms: int


def _duplicate_index(envelope: InternalEnvelope) -> int:
    """The instance's ``duplicate_index`` (0 original, ≥1 copy) from ``_df.chaos``."""
    chaos = envelope["_df"].get("chaos")
    if not chaos:
        return 0
    dup = chaos.get("duplicates")
    if isinstance(dup, dict):
        return int(cast(int, dup.get("duplicate_index", 0)))
    return 0


def _parse_emitted_at(emitted_at: str) -> datetime:
    return datetime.fromisoformat(emitted_at.replace("Z", "+00:00"))


def _format_wall(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class LateArrivingStage:
    """The ``late_arriving`` mode stage (§5.7) — terminal, buffer-extracting."""

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
        delay_spec = compile_dwell(cast(dict[str, object], params.get("delay", _DEFAULT_DELAY)))
        max_delay_us = self._max_delay_us(params)
        k = self._speed_multiplier(ctx)
        out: list[InternalEnvelope] = []
        for envelope in batch:
            if not event_type_eligible(envelope["event_type"], selector):
                out.append(envelope)
                continue
            event_id = envelope["event_id"]
            dup = _duplicate_index(envelope)
            if draw_u(ctx.chaos_subseed, MODE, event_id, "select", dup) >= rate:
                out.append(envelope)
                continue
            self._extract(envelope, ctx, event_id, dup, delay_spec, max_delay_us, k)
        return out

    def _extract(
        self,
        envelope: InternalEnvelope,
        ctx: StageContext,
        event_id: str,
        dup: int,
        delay_spec: object,
        max_delay_us: int,
        k: float,
    ) -> None:
        """Record the injection (pending) then insert the buffer entry (INV-CHA-4)."""
        delay_u = draw_u(ctx.chaos_subseed, MODE, event_id, "delay", dup)
        delay_us = self._draw_delay_us(delay_spec, delay_u, max_delay_us)
        delay_ms = delay_us // _US_PER_MS
        wall_delay_s = (delay_us / 1_000_000.0) / k
        due_at_dt = _parse_emitted_at(envelope["emitted_at"]) + timedelta(seconds=wall_delay_s)
        due_at = _format_wall(due_at_dt)
        injection_id = deterministic_injection_id(
            ctx.chaos_subseed, MODE, event_id, envelope["occurred_at"], dup
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
            "details": {
                "delay_simulated_ms": delay_ms,
                "due_at_wall": due_at,
                "outcome": "pending",
                "duplicate_index": dup,
            },
        }
        ctx.recorder.record(record)  # BEFORE the buffer insert / extraction (INV-CHA-4)
        stored = clone_envelope(envelope)
        label_touched(
            stored,
            injection_id,
            MODE,
            {"delay_simulated_ms": delay_ms, "due_at_wall": due_at},
        )
        entry: ScheduledEntry = {
            "workspace_id": ctx.workspace_id,
            "stream_id": ctx.stream_id,
            "shard_id": ctx.shard_id,
            "injection_id": injection_id,
            "event_id": event_id,
            "envelope": stored,
            "due_at": due_at,
            "delay_simulated_ms": delay_ms,
        }
        if ctx.late_buffer is not None:
            ctx.late_buffer.insert(entry)

    @staticmethod
    def _draw_delay_us(delay_spec: object, u: float, max_delay_us: int) -> int:
        spec = delay_spec
        if getattr(spec, "needs_draw", True):
            raw = spec.sample(u)  # type: ignore[attr-defined]
        else:
            raw = spec.sample_fixed_value()  # type: ignore[attr-defined]
        return min(int(raw), max_delay_us)

    @staticmethod
    def _max_delay_us(params: dict[str, object]) -> int:
        from dataforge_engine.behavior.distributions import parse_duration_us

        raw = params.get("max_delay", _DEFAULT_MAX_DELAY)
        return parse_duration_us(str(raw))

    @staticmethod
    def _speed_multiplier(ctx: StageContext) -> float:
        """``k`` from the context's virtual clock if present, else 1.0 (§3.4)."""
        clock = ctx.virtual_clock
        k = getattr(clock, "speed_multiplier", 1.0) if clock is not None else 1.0
        try:
            return float(k) if float(k) > 0 else 1.0
        except (TypeError, ValueError):
            return 1.0
