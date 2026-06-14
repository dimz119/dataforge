"""WallClock port adapters (engine port :class:`dataforge_engine.ports.WallClock`).

The engine reads wall time only through this injected port (BE-ENG-2), used for
``emitted_at`` stamping and token-bucket refill. Two host adapters:

* :class:`SystemWallClock` — production / batch generation: wraps
  ``datetime.now(UTC)``.
* :class:`DeterministicWallClock` — the golden harness (testing-strategy §6):
  advances a fixed step per ``now()`` call from a pinned epoch, so ``emitted_at``
  is byte-stable across runs and the full envelope (incl. wall fields) reproduces
  identically (GOLD-A).

Both return tz-aware UTC ``datetime``. Pure Python (no Django import needed; this
module is the host seam, not the engine).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["DeterministicWallClock", "SystemWallClock"]


class SystemWallClock:
    """The production wall clock: ``datetime.now(UTC)`` (tz-aware)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class DeterministicWallClock:
    """A pinned, monotonically-advancing wall clock for golden replay (GOLD-A).

    Each :meth:`now` returns ``epoch + step * call_count`` then increments the
    counter, so a fixed-seed batch produces a byte-identical ``emitted_at``
    sequence regardless of real wall time, batch size, or pass boundaries.
    """

    __slots__ = ("_count", "_epoch", "_step")

    def __init__(
        self,
        *,
        epoch: datetime | None = None,
        step: timedelta = timedelta(milliseconds=1),
    ) -> None:
        base = epoch or datetime(2026, 1, 1, tzinfo=UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        self._epoch = base.astimezone(UTC)
        self._step = step
        self._count = 0

    def now(self) -> datetime:
        instant = self._epoch + self._step * self._count
        self._count += 1
        return instant
