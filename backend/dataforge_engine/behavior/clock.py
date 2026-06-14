"""Virtual clock and the generation frontier (behavior-engine §3.1).

The per-stream virtual clock maps simulated microseconds-since-``virtual_epoch``
to canonical instants for ``occurred_at`` stamping, and (in live mode) computes
``virtual_now`` from the injected wall clock under run segments. The engine adds
the **generation frontier ``F``** — the virtual time up to and including which the
shard has processed timers (BE-C1..C4).

Every emitted event stamps ``occurred_at`` from the *virtual due time of its
timer*, never from ``virtual_now`` at processing (BE-C2) — the single rule that
makes content independent of wall pacing.

Pure Python; ``datetime`` only (BE-ENG-1). All instants are tz-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

_US_PER_MS = 1000
_MS_PER_SECOND = 1000

# The Unix epoch as a tz-aware datetime — the base for µs/ms ↔ datetime mapping.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def virtual_epoch_ms(virtual_epoch: datetime) -> int:
    """``virtual_epoch`` as integer epoch milliseconds (the simulated-time origin)."""
    return int(virtual_epoch.timestamp() * _MS_PER_SECOND)


def simulated_instant(virtual_epoch_ms_value: int, frontier_us: int) -> datetime:
    """Map a frontier offset (simulated µs since epoch) to a UTC ``datetime``.

    ``occurred_at`` is built from this: the timer's ``virtual_due_at`` (µs) plus
    the epoch gives the simulated instant, formatted to RFC 3339 by the envelope
    builder (which truncates to ms for ``event_id`` bits).
    """
    total_us = virtual_epoch_ms_value * _US_PER_MS + frontier_us
    return _EPOCH + timedelta(microseconds=total_us)


def format_simulated_ms(epoch_ms: int) -> str:
    """An epoch-ms instant → RFC 3339 string (``time.between`` / ``time.now``)."""
    from dataforge_engine.envelope import format_rfc3339
    return format_rfc3339(_EPOCH + timedelta(milliseconds=epoch_ms))


@dataclass
class Segment:
    """One run segment: wall + virtual anchors and the pinned speed multiplier.

    ``virtual_now = v_anchor_us + k x (wall_now - w_anchor)``. A new segment opens
    at stream start and at every resume (anchored at ``(wall_resume,
    frontier_us)``; behavior-engine §9.3 step 4).
    """

    wall_anchor: datetime
    virtual_anchor_us: int  # simulated µs since virtual_epoch
    speed_multiplier: float


class VirtualClock:
    """The per-stream virtual clock + frontier.

    In ``live`` mode :meth:`virtual_now_us` reads the injected wall clock through
    the active segment. In ``backfill`` mode ``virtual_now`` is undefined and the
    frontier advances as fast as generation allows (§8); :meth:`virtual_now_us`
    raises if called.
    """

    def __init__(
        self,
        *,
        virtual_epoch: datetime,
        speed_multiplier: float = 1.0,
        mode: str = "live",
        frontier_us: int = 0,
    ) -> None:
        self.virtual_epoch = virtual_epoch
        self.virtual_epoch_ms = virtual_epoch_ms(virtual_epoch)
        self.speed_multiplier = speed_multiplier
        self.mode = mode
        self.frontier_us = frontier_us
        self._segment: Segment | None = None

    @property
    def is_backfill(self) -> bool:
        return self.mode == "backfill"

    def open_segment(self, wall_now: datetime) -> None:
        """Open a run segment anchored at ``(wall_now, frontier_us)`` (start/resume)."""
        self._segment = Segment(wall_now, self.frontier_us, self.speed_multiplier)

    def virtual_now_us(self, wall_now: datetime) -> int:
        """``virtual_now`` in simulated µs since epoch (live mode only)."""
        if self.is_backfill:
            raise RuntimeError("virtual_now is undefined in backfill mode (BE-C1)")
        if self._segment is None:
            self.open_segment(wall_now)
        seg = self._segment
        assert seg is not None
        wall_delta_us = (wall_now - seg.wall_anchor) / timedelta(microseconds=1)
        return seg.virtual_anchor_us + int(seg.speed_multiplier * wall_delta_us)

    def advance_frontier(self, virtual_due_at_us: int) -> None:
        """Advance ``F`` to a processed timer's due time (monotone, BE-C3)."""
        if virtual_due_at_us > self.frontier_us:
            self.frontier_us = virtual_due_at_us

    def instant_for(self, virtual_due_at_us: int) -> datetime:
        """The ``occurred_at`` instant for a timer due at ``virtual_due_at_us``."""
        return simulated_instant(self.virtual_epoch_ms, virtual_due_at_us)
