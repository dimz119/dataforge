"""The dry-run recorder — accumulates realized metrics for L3 (plugin-arch §8.4).

A concrete :class:`~dataforge_engine.behavior.observer.Observer` plus an emitted-event
tally. It records exactly what the §8.3 ``dry_run`` block needs and what static L1+L2
cannot see: per-transition selection/guard counts (for ``realized_rates`` and W-D610),
completed-session count (the 1,000-traversal cap), per-session event counts (for
``mean_events_per_session``), per-event-type counts (W-D611), referenced entities
(W-D612), and the payload-size distribution (``max``/``p99`` and MAN-D605).

Generic by construction: it keys everything on machine/state/transition-index and
entity/event-type names from the IR — no scenario knowledge (BE-T1). It performs no
draws and no mutations, so attaching it never perturbs determinism.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dataforge_engine.envelope import InternalEnvelope

    from .ir import ManifestIR

__all__ = ["DryRunRecorder"]


class DryRunRecorder:
    """Accumulates the realized-behavior signals one L3 dry run reports."""

    def __init__(self, ir: ManifestIR) -> None:
        self._ir = ir
        # (machine, state, transition_index) → [selected_count, guard_pass_count].
        self._selections: dict[tuple[str, str, int], list[int]] = {}
        # (machine, state) → remainder-policy selections (index -1).
        self._remainders: dict[tuple[str, str], int] = {}
        self.sessions_completed = 0
        self.business_events = 0
        self.session_events = 0  # business events carrying a non-null session_id
        self._event_type_counts: dict[str, int] = {}
        self._referenced: set[str] = set()
        self._payload_sizes: list[int] = []  # kept sorted for the p99 quantile
        self.max_payload = 0

    # -- Observer protocol (called from the interpreter hot path) ------------

    def on_select(self, machine: str, state: str, transition_index: int) -> None:
        if transition_index < 0:
            key = (machine, state)
            self._remainders[key] = self._remainders.get(key, 0) + 1
            return
        slot = self._selections.setdefault((machine, state, transition_index), [0, 0])
        slot[0] += 1

    def on_guard(self, machine: str, state: str, transition_index: int, *, passed: bool) -> None:
        if passed:
            slot = self._selections.setdefault((machine, state, transition_index), [0, 0])
            slot[1] += 1

    def on_session_complete(self, traversal_id: str) -> None:
        self.sessions_completed += 1

    # -- emitted-event tally (called per generated pass) --------------------

    def note_head(self, batch: Sequence[InternalEnvelope]) -> None:
        """Record the head ``op:'r'`` snapshot rows (entity references, sizes)."""
        self.observe_batch(batch, business_only=False)

    def observe_batch(
        self, batch: Sequence[InternalEnvelope], *, business_only: bool = False
    ) -> None:
        for env in batch:
            event_type = str(env["event_type"])
            size = self._record_payload_size(env)
            is_business = not event_type.startswith("cdc.")
            if is_business:
                self.business_events += 1
                self._event_type_counts[event_type] = (
                    self._event_type_counts.get(event_type, 0) + 1
                )
                if env.get("session_id") is not None:
                    self.session_events += 1
            self._record_refs(env)
            if business_only and not is_business:
                self._payload_sizes.pop()  # undo the size record we just added
                if size == self.max_payload:
                    self.max_payload = self._payload_sizes[-1] if self._payload_sizes else 0

    def _record_payload_size(self, env: InternalEnvelope) -> int:
        size = _payload_size(env)
        bisect.insort(self._payload_sizes, size)
        if size > self.max_payload:
            self.max_payload = size
        return size

    def _record_refs(self, env: InternalEnvelope) -> None:
        for ref in env.get("entity_refs", []) or []:
            etype = ref.get("entity_type") if isinstance(ref, dict) else None
            if isinstance(etype, str):
                self._referenced.add(etype)
        # CDC events reference their own entity type ("cdc.<entity>").
        event_type = str(env["event_type"])
        if event_type.startswith("cdc."):
            self._referenced.add(event_type[len("cdc.") :])

    # -- queries the dry-run finalizer asks ---------------------------------

    def guard_stats(self, machine: str, state: str, transition_index: int) -> tuple[int, int]:
        """``(selected_count, guard_pass_count)`` for one transition (W-D610)."""
        slot = self._selections.get((machine, state, transition_index))
        return (slot[0], slot[1]) if slot is not None else (0, 0)

    def realized_rates(self) -> dict[str, float]:
        """Per-transition realized selection rate (§8.3 ``realized_rates``).

        Rate = selections of this transition / total decisions in its state (all
        transitions + remainder fall-through), so it conditions on guard-pass exactly
        as §6.2 specifies. Keyed ``machine.state.to_state`` like the §8.3 example.
        """
        totals: dict[tuple[str, str], int] = {}
        for (machine, state, _idx), slot in self._selections.items():
            totals[(machine, state)] = totals.get((machine, state), 0) + slot[0]
        for (machine, state), count in self._remainders.items():
            totals[(machine, state)] = totals.get((machine, state), 0) + count
        rates: dict[str, float] = {}
        for (machine, state, idx), slot in self._selections.items():
            denom = totals.get((machine, state), 0)
            if denom == 0:
                continue
            to_state = self._ir.machines[machine].states[state].transitions[idx].to
            rates[f"{machine}.{state}.{to_state}"] = round(slot[0] / denom, 4)
        return rates

    def event_type_count(self, event_type: str) -> int:
        return self._event_type_counts.get(event_type, 0)

    def entity_referenced(self, entity: str) -> bool:
        return entity in self._referenced

    def p99_payload(self) -> int:
        """The 99th-percentile payload byte size (nearest-rank over the sorted list)."""
        if not self._payload_sizes:
            return 0
        rank = max(0, round(0.99 * (len(self._payload_sizes) - 1)))
        return self._payload_sizes[rank]


def _payload_size(env: InternalEnvelope) -> int:
    import json
    from decimal import Decimal

    def _default(value: Any) -> str:
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError(type(value).__name__)

    body = json.dumps(
        env["payload"], separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_default,
    )
    return len(body.encode("utf-8"))
