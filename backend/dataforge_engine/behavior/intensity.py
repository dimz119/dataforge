"""Intensity curves — diurnal/weekly arrival-rate modulation (behavior-engine §3.4).

Curves come from the manifest ``intensity`` section (overridable per scenario
instance) and modulate **session arrival rate only** (PRD §4.3); per-session pacing
stays dwell-driven. Two binding properties make them safe to ship:

* **Renormalization to mean 1.0** — the 24 diurnal hour-values ``d(h)`` are divided
  by ``mean₂₄(d)`` and the 7 weekly day-values ``w(j)`` by ``mean₇(w)`` (simple
  averages). Consequence (unit-tested, testing-strategy §3): *changing the curve
  shape never changes average throughput* — ``target_tps`` stays the exact daily
  average. A manifest that omits ``diurnal``/``weekly`` is flat ``1.0``.
* **Closed-form evaluation** — ``intensity(v) = d'(hour_local(v)) x w'(dow_local(v))``
  evaluated at the arrival's virtual time in the instance ``simulated_timezone``.
  The product is piecewise-constant with breakpoints at simulated hour and day
  boundaries, which is exactly what makes arrival-time inversion closed-form (§3.5).

Multiplier bounds ``[0, 10]`` (B-15); a zero-intensity span schedules no arrivals.

Pure Python; ``datetime`` + ``zoneinfo`` (stdlib) only (BE-ENG-1). All inputs are
manifest **data** — zero scenario logic enters the runtime (ADR-0003 GUARD).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["IntensityCurve", "compile_intensity"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_HOURS = 24
_DAYS = 7
# Monday-first weekday order; matches datetime.weekday() (Mon=0 … Sun=6).
_WEEK_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MULT_MIN = 0.0
_MULT_MAX = 10.0


def _clamp(value: float) -> float:
    """Clamp a manifest multiplier into the [0, 10] bound (B-15)."""
    return max(_MULT_MIN, min(_MULT_MAX, value))


def _renormalize(values: list[float]) -> tuple[float, ...]:
    """Divide each value by the simple mean so the sequence averages to 1.0.

    A degenerate all-zero curve (mean 0) renormalizes to flat 1.0 — a curve that
    never schedules anything would otherwise zero out the whole stream, which no
    valid manifest expresses (B-15 keeps at least one positive bucket).
    """
    mean = sum(values) / len(values)
    if mean <= 0.0:
        return tuple(1.0 for _ in values)
    return tuple(v / mean for v in values)


@dataclass(frozen=True)
class IntensityCurve:
    """The compiled, renormalized diurnal x weekly curve (§3.4).

    ``diurnal`` is 24 per-hour multipliers (renormalized to mean 1.0), ``weekly`` is
    7 per-weekday multipliers (Mon-first, renormalized to mean 1.0). ``tz`` is the
    instance ``simulated_timezone`` the curve evaluates against. ``is_flat`` is the
    fast path: a manifest with no ``intensity`` section is flat 1.0 everywhere, so
    the arrival inversion stays a single division (§3.5).
    """

    diurnal: tuple[float, ...]
    weekly: tuple[float, ...]
    tz_name: str
    is_flat: bool

    def at(self, virtual_us: int, virtual_epoch_ms: int) -> float:
        """``intensity(v)`` — the renormalized multiplier at simulated µs ``v``.

        Maps ``v`` (simulated µs since epoch) to a tz-aware instant in the
        ``simulated_timezone``, then reads the diurnal hour bucket x weekly day
        bucket. Piecewise-constant with breakpoints at local hour/day edges.
        """
        if self.is_flat:
            return 1.0
        local = self._local(virtual_us, virtual_epoch_ms)
        return self.diurnal[local.hour] * self.weekly[local.weekday()]

    def _local(self, virtual_us: int, virtual_epoch_ms: int) -> datetime:
        total_us = virtual_epoch_ms * 1000 + virtual_us
        instant = _EPOCH + timedelta(microseconds=total_us)
        return instant.astimezone(self._tz())

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            return ZoneInfo("UTC")


def _expand_diurnal(buckets: list[dict[str, Any]]) -> list[float]:
    """Expand contiguous ``[from_hour, to_hour)`` buckets to 24 per-hour values.

    Buckets contiguously cover [0, 24) (Layer-2 rule MAN-V315). An hour not covered
    (defensive — never for a validated manifest) defaults to 1.0.
    """
    hours = [1.0] * _HOURS
    for bucket in buckets:
        frm = int(bucket.get("from_hour", 0))
        to = int(bucket.get("to_hour", 0))
        mult = _clamp(float(bucket.get("multiplier", 1.0)))
        for h in range(max(0, frm), min(_HOURS, to)):
            hours[h] = mult
    return hours


def _expand_weekly(weekly: dict[str, Any]) -> list[float]:
    """Expand the ``{mon..sun}`` map to 7 Mon-first per-weekday values."""
    return [_clamp(float(weekly.get(key, 1.0))) for key in _WEEK_KEYS]


def compile_intensity(intensity: dict[str, Any] | None, *, tz_name: str = "UTC") -> IntensityCurve:
    """Compile a manifest ``intensity`` section into a renormalized curve.

    ``None`` or an empty section → flat 1.0 (no curve declared). A declared
    ``diurnal``/``weekly`` is expanded then renormalized to mean 1.0 so the daily
    average never moves (§3.4). The result is immutable and cheap to evaluate.
    """
    section = intensity or {}
    diurnal_raw = section.get("diurnal")
    weekly_raw = section.get("weekly")
    diurnal_buckets: list[dict[str, Any]] = (
        diurnal_raw if isinstance(diurnal_raw, list) and diurnal_raw else []
    )
    weekly_map: dict[str, Any] = (
        weekly_raw if isinstance(weekly_raw, dict) and weekly_raw else {}
    )
    if not diurnal_buckets and not weekly_map:
        return IntensityCurve(
            diurnal=tuple([1.0] * _HOURS),
            weekly=tuple([1.0] * _DAYS),
            tz_name=tz_name,
            is_flat=True,
        )
    diurnal = (
        _renormalize(_expand_diurnal(diurnal_buckets))
        if diurnal_buckets
        else tuple([1.0] * _HOURS)
    )
    weekly = (
        _renormalize(_expand_weekly(weekly_map))
        if weekly_map
        else tuple([1.0] * _DAYS)
    )
    return IntensityCurve(diurnal=diurnal, weekly=weekly, tz_name=tz_name, is_flat=False)
