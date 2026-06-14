"""Closed distribution-sampling catalog (behavior-engine §7.3).

Every sample consumes **exactly one** uniform draw ``u`` (``fixed`` consumes
zero) — fixed-draw accounting is what makes cursors restorable (§9.1) and content
additive-change-safe (§7.1). The dwell families come from the manifest
``distribution`` grammar (plugin-architecture §9.1); the numeric/discrete
generator families are sampled by :mod:`.generators` using the same primitives.

All durations are returned as **integer simulated microseconds** (the heap's
``virtual_due_at`` unit, §3.2), clamped at the ``P365D`` ceiling (B-15) by the
caller. Pure Python; ``math``/``statistics`` (stdlib) only (BE-ENG-1).
"""

from __future__ import annotations

import math
import re
from statistics import NormalDist

# ---------------------------------------------------------------------------
# ISO-8601 duration parsing (the manifest ``duration`` grammar, §9.1).
# ---------------------------------------------------------------------------

# P[nD][T[nH][nM][nS]] — the §9.1 $defs/duration pattern, with fractional seconds.
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)

_US_PER_SECOND = 1_000_000
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86_400

# B-15: every dwell/timeout/window ≤ P365D. The deterministic dwell ceiling.
DWELL_CEILING_US = 365 * _SECONDS_PER_DAY * _US_PER_SECOND

# §7.3 lognormal: sigma = ln(p95/median) / Φ⁻¹(0.95); Φ⁻¹(0.95) is this constant.
_Z95 = 1.6448536269514722
_NORMAL = NormalDist()
# §7.3 open-interval guard for inv_cdf: clamp u to (2^-64, 1 - 2^-53).
_U_LO = 2.0**-64
_U_HI = 1.0 - 2.0**-53


class DistributionError(ValueError):
    """Raised on a malformed duration string or unknown distribution family."""


def parse_duration_us(value: str) -> int:
    """Parse an ISO-8601 duration (``PT3M``, ``P4D``, ``PT1.5S``) → integer µs."""
    match = _DURATION_RE.match(value)
    if match is None or value == "P":
        raise DistributionError(f"malformed ISO-8601 duration {value!r} (§9.1)")
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0.0)
    total_seconds = (
        days * _SECONDS_PER_DAY
        + hours * _SECONDS_PER_HOUR
        + minutes * _SECONDS_PER_MINUTE
        + seconds
    )
    return round(total_seconds * _US_PER_SECOND)


# ---------------------------------------------------------------------------
# The dwell families (§7.3), each sampling from one uniform ``u``.
# ---------------------------------------------------------------------------


def sample_fixed(value_us: int) -> int:
    """``fixed`` — the constant; consumes no draw (caller must not advance)."""
    return value_us


def sample_uniform(u: float, min_us: int, max_us: int) -> int:
    """``uniform`` — ``min + u x (max - min)`` (§7.3)."""
    return round(min_us + u * (max_us - min_us))


def sample_exponential(u: float, mean_us: float) -> int:
    """``exponential`` — ``-mean x ln(1 - u)`` (§7.3)."""
    return round(-mean_us * math.log(1.0 - _clamp_for_log(u)))


def sample_lognormal(u: float, median_us: float, p95_us: float) -> int:
    """``lognormal`` — ``exp(ln(median) + sigma·Φ⁻¹(u))`` (§7.3).

    ``sigma = ln(p95/median) / 1.6448536269514722``; ``u`` clamped to the open
    interval before ``inv_cdf``.
    """
    sigma = math.log(p95_us / median_us) / _Z95
    z = _NORMAL.inv_cdf(_clamp_open(u))
    return round(math.exp(math.log(median_us) + sigma * z))


def _clamp_for_log(value: float) -> float:
    # 1 - u must stay in (0, 1] for ln; guard the u → 1 boundary.
    if value >= _U_HI:
        return _U_HI
    return value


def _clamp_open(value: float) -> float:
    if value <= _U_LO:
        return _U_LO
    if value >= _U_HI:
        return _U_HI
    return value


# ---------------------------------------------------------------------------
# Compiled dwell spec — bound once at IR compile, sampled per selection.
# ---------------------------------------------------------------------------


class DwellSpec:
    """A compiled ``distribution`` ready to sample (one draw, clamped at B-15).

    ``family == "fixed"`` consumes no draw — :meth:`needs_draw` is ``False`` and
    :meth:`sample_fixed_value` returns the constant. Every other family takes one
    uniform ``u`` and returns clamped microseconds.
    """

    __slots__ = ("family", "p0", "p1")

    def __init__(self, family: str, p0: int, p1: int) -> None:
        self.family = family
        self.p0 = p0  # fixed value / uniform.min / lognormal.median / exp.mean
        self.p1 = p1  # uniform.max / lognormal.p95 (unused for fixed/exponential)

    @property
    def needs_draw(self) -> bool:
        return self.family != "fixed"

    def sample_fixed_value(self) -> int:
        return min(self.p0, DWELL_CEILING_US)

    def sample(self, u: float) -> int:
        if self.family == "uniform":
            raw = sample_uniform(u, self.p0, self.p1)
        elif self.family == "lognormal":
            raw = sample_lognormal(u, self.p0, self.p1)
        elif self.family == "exponential":
            raw = sample_exponential(u, self.p0)
        else:  # pragma: no cover - guarded at compile time
            raise DistributionError(f"unknown dwell family {self.family!r}")
        return min(max(raw, 0), DWELL_CEILING_US)


# Default dwell when a transition omits ``dwell`` (plugin-architecture §6.2 rule 4).
DEFAULT_DWELL = DwellSpec("fixed", 0, 0)


def compile_dwell(spec: dict[str, object] | None) -> DwellSpec:
    """Compile a manifest ``dwell`` mapping into a :class:`DwellSpec` (or default)."""
    if not spec:
        return DEFAULT_DWELL
    family = str(spec.get("family"))
    if family == "fixed":
        return DwellSpec("fixed", parse_duration_us(str(spec["value"])), 0)
    if family == "uniform":
        return DwellSpec(
            "uniform", parse_duration_us(str(spec["min"])), parse_duration_us(str(spec["max"]))
        )
    if family == "lognormal":
        return DwellSpec(
            "lognormal",
            parse_duration_us(str(spec["median"])),
            parse_duration_us(str(spec["p95"])),
        )
    if family == "exponential":
        return DwellSpec("exponential", parse_duration_us(str(spec["mean"])), 0)
    raise DistributionError(f"unknown dwell family {family!r} (§9.1)")
