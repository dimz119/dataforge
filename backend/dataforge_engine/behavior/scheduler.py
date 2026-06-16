"""Timer heap, arrival process, and token-bucket pacing (behavior-engine §3.2,
§3.5, §3.6).

The timer heap is one binary min-heap per shard with a frozen total order:
``(virtual_due_at, timer_seq)`` (§3.2) — checkpoint restore and determinism depend
on it. The arrival process realizes an inhomogeneous Poisson process by inversion
over integrated intensity (§3.5); with flat intensity 1.0 (curves are Phase 8) the
solve is a single division. The token bucket paces wall-side throughput (§3.6) and
never affects content (BE-C2).

Pure Python; ``heapq`` (stdlib) only (BE-ENG-1).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from .intensity import IntensityCurve
    from .rng import Cursor

# Arrival inversion steps the piecewise-constant intensity by simulated hour
# boundaries (the finest breakpoint of d'(h) x w'(j); day edges are hour edges).
_US_PER_HOUR = 3_600 * 1_000_000
# Hard cap on segment steps per arrival solve — a positive-but-tiny intensity over
# a very low rho could otherwise walk many hours; bounded so the solve terminates.
_MAX_SOLVE_HOURS = 24 * 366

TimerKind = Literal[
    "arrival", "dwell", "state_timeout", "session_timeout", "background_day", "bg_mutation"
]


@dataclass(order=True)
class Timer:
    """One heap entry (§3.2). Ordered by ``(virtual_due_at, timer_seq)``.

    ``ref`` carries the traversal id / arrival index / background-rule reference;
    it is excluded from ordering (``compare=False``) so the total order is exactly
    the two key fields.
    """

    virtual_due_at: int
    timer_seq: int
    kind: TimerKind = field(compare=False)
    ref: dict[str, Any] = field(compare=False, default_factory=dict)


class TimerHeap:
    """The per-shard min-heap with a checkpointable ``timer_seq`` counter."""

    def __init__(self, timer_seq_next: int = 0) -> None:
        self._heap: list[Timer] = []
        self._timer_seq_next = timer_seq_next

    @property
    def timer_seq_next(self) -> int:
        return self._timer_seq_next

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, virtual_due_at: int, kind: TimerKind, ref: dict[str, Any]) -> Timer:
        timer = Timer(virtual_due_at, self._timer_seq_next, kind, ref)
        self._timer_seq_next += 1
        heapq.heappush(self._heap, timer)
        return timer

    def push_existing(self, timer: Timer) -> None:
        """Restore a checkpointed timer without re-assigning its ``timer_seq``."""
        heapq.heappush(self._heap, timer)
        if timer.timer_seq >= self._timer_seq_next:
            self._timer_seq_next = timer.timer_seq + 1

    def peek(self) -> Timer | None:
        return self._heap[0] if self._heap else None

    def pop(self) -> Timer:
        return heapq.heappop(self._heap)

    def entries(self) -> list[Timer]:
        """All live entries (for checkpoint serialization, §9.1)."""
        return list(self._heap)

    def pending_refs(self, ref_key: str) -> frozenset[str]:
        """Distinct ``ref[ref_key]`` values across live timers (archival §4.4)."""
        return frozenset(
            str(t.ref[ref_key]) for t in self._heap if ref_key in t.ref
        )


# ---------------------------------------------------------------------------
# Arrival process (§3.5) — inversion over integrated intensity.
# ---------------------------------------------------------------------------


@dataclass
class ArrivalState:
    """The checkpointable arrival integrator position (§3.5; §9.1 ``arrival``)."""

    next_index: int = 0
    solve_from_us: int = 0
    gap_remaining: float = 0.0  # partially-integrated mass carried across segments


class ArrivalProcess:
    """Realizes per-shard session arrivals by inversion sampling (§3.5).

    With flat intensity 1.0 (curves Phase 8), ``λ(v) = rho`` is constant within a
    TPS-schedule step, so an arrival lands at ``v_prev + E/rho``. ``rho_fn`` returns
    the base density rho (sessions per simulated second) at a virtual time; the
    caller supplies it from the TPS schedule (live) or population (backfill).
    """

    def __init__(self, cursor: Cursor, state: ArrivalState | None = None) -> None:
        self._cursor = cursor
        self.state = state or ArrivalState()

    def rebase_cursor(self) -> None:
        """Re-anchor the gap-draw cursor to the restored ``next_index`` (§9.3 restore).

        The arrival cursor consumes exactly one exponential-gap draw per arrival
        (``next_arrival_us`` advances the cursor and ``next_index`` in lockstep), so
        ``cursor.position`` is an invariant equal to ``state.next_index``. The
        checkpoint blob records ``next_index`` but not the cursor position (it is
        derivable, §9.1 "RNG cursor positions"); a restored shard builds a *fresh*
        cursor at position 0, so restore MUST rebase it here or the next inter-arrival
        gap is drawn at the wrong position — silently diverging the arrival schedule
        and thus every downstream session (the GOLD-D continuation defect)."""
        self._cursor.position = self.state.next_index

    def next_arrival_us(self, rho: float) -> int | None:
        """The next arrival's virtual µs at constant density ``rho``, or ``None``.

        ``None`` when ``rho <= 0`` (a zero-rate span schedules no arrivals, §3.4).
        Consumes one exponential gap draw keyed on the arrival index (§7.1).
        """
        if rho <= 0.0:
            return None
        u = self._cursor.u()
        gap_mass = -math.log(1.0 - min(u, 1.0 - 2.0**-53))
        # integrated intensity = rho x Δseconds = gap_mass ⇒ Δus = gap_mass/rho x 1e6
        delta_us = int(gap_mass / rho * 1_000_000)
        due = self.state.solve_from_us + delta_us
        self.state.next_index += 1
        self.state.solve_from_us = due
        return due

    def next_arrival_us_curved(
        self,
        rho: float,
        curve: IntensityCurve,
        virtual_epoch_ms: int,
    ) -> int | None:
        """Next arrival µs under ``λ(v) = rho x intensity(v)`` (§3.4/§3.5 step 2).

        Solves ``∫_{vₙ₋₁}^{vₙ} λ(v) dv = Eₙ`` over the piecewise-constant curve by
        stepping simulated-hour segments — the finest breakpoint of ``d'(h) x w'(j)``
        (day edges coincide with hour edges). Within a segment of rate ``λₛ`` and
        remaining mass ``E``: if ``λₛ.Δ ≥ E`` the arrival lands at ``v + E/λₛ``; else
        subtract ``λₛ.Δ`` and step to the next hour. Zero-intensity hours are skipped
        (they schedule no arrivals — they contribute no mass). The flat-curve fast
        path delegates to :meth:`next_arrival_us` (a single division), so the
        renormalized mean-1.0 curve reproduces the flat schedule on average exactly.
        """
        if rho <= 0.0:
            return None
        if curve.is_flat:
            return self.next_arrival_us(rho)
        u = self._cursor.u()
        gap_mass = -math.log(1.0 - min(u, 1.0 - 2.0**-53))
        self.state.next_index += 1
        v = self.state.solve_from_us
        remaining = gap_mass
        for _ in range(_MAX_SOLVE_HOURS):
            intensity = curve.at(v, virtual_epoch_ms)
            rate = rho * intensity  # sessions per simulated second within the hour
            # Δ to the next hour boundary in simulated seconds.
            hour_end_us = (v // _US_PER_HOUR + 1) * _US_PER_HOUR
            delta_seconds = (hour_end_us - v) / 1_000_000
            segment_mass = rate * delta_seconds
            if rate > 0.0 and segment_mass >= remaining:
                v += int(remaining / rate * 1_000_000)
                self.state.solve_from_us = v
                return v
            remaining -= segment_mass
            v = hour_end_us
        # Exhausted the step budget (vanishingly rare): land at the budget edge so
        # the schedule stays deterministic rather than raising.
        self.state.solve_from_us = v
        return v


# ---------------------------------------------------------------------------
# Token bucket (§3.6) — wall-domain pacing.
# ---------------------------------------------------------------------------


class TokenBucket:
    """Per-shard wall-domain token bucket (§3.6).

    Rate = ``target_tps / shard_count`` tokens/s; capacity = ``max(2xrate, 1)``.
    Every canonical event costs one token. Refill is continuous from the injected
    wall clock. A starved pass does not advance (events never dropped). Pacing
    never touches content (BE-C2), so backfill mode skips the bucket entirely.
    """

    def __init__(self, *, rate_per_second: float, now: datetime) -> None:
        self.rate = rate_per_second
        self.capacity = max(2.0 * rate_per_second, 1.0)
        self.tokens = self.capacity
        self._last = now

    def refill(self, now: datetime) -> None:
        from datetime import timedelta
        elapsed = (now - self._last) / timedelta(seconds=1)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self._last = now

    def grant(self, now: datetime) -> int:
        """Whole tokens available this pass (the ``budget`` for ``generate``)."""
        self.refill(now)
        return int(self.tokens)

    def consume(self, count: int) -> None:
        self.tokens = max(0.0, self.tokens - count)

    def set_rate(self, rate_per_second: float) -> None:
        """Adopt a new rate at the next tick poll (BE-P2)."""
        self.rate = rate_per_second
        self.capacity = max(2.0 * rate_per_second, 1.0)
        self.tokens = min(self.tokens, self.capacity)
