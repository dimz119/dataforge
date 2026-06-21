"""STAT-C1..C7 — per-mode chaos realized-rate + late-delay statistics (§5.3, §11).

Phase-9 exit criterion #1 (merge + gate run): a configured 5 % rate realizes
5 % ± 1 % over 50,000 canonical events for EVERY mode (the absolute ±1 pp / ±10 %
relative tolerance of §11), and ``late_arriving`` honors ``occurred_at`` < the
delivered ``emitted_at`` with the realized simulated-delay median within ±15 % of
the configured median (and the wall realization correct at k = 60).

Two profiles over the *deterministic chaos projection* (no broker, no DB):

* **PR-smoke** (``stat`` marker): each mode at 5 % over 10,000 events (≈ a few s) —
  the early per-mode realized-rate signal on the unit lane.
* **Nightly / Phase-9 gate** (``stat_nightly`` marker): the binding n = 50,000.

Determinism (TP-2) means these are NOT flaky: a tolerance breach is a real chaos-
rate regression, never retried. The realized rate is exact for a given seed; the
tolerance only absorbs the seed-to-seed sampling spread the spec budgets for.
"""

from __future__ import annotations

import statistics
from typing import cast

import pytest

from dataforge_engine.chaos import default_policy
from tests.chaos.projection import Projection, run_projection

SMOKE_N = 10_000
GATE_N = 50_000
RATE = 0.05
# §11: max(±1 pp absolute, ±10 % relative). At rate 0.05 that is the ±1 pp band.
ABS_TOL = 0.01
_30M_MS = 30 * 60 * 1000

_MODES = (
    "missing",
    "duplicates",
    "corrupted_values",
    "nulls",
    "schema_drift",
    "out_of_order",
    "late_arriving",
)


def _single_mode_policy(mode: str, rate: float = RATE) -> object:
    policy = default_policy()
    policy[mode]["enabled"] = True  # type: ignore[literal-required]
    policy[mode]["rate"] = rate  # type: ignore[literal-required]
    if mode == "late_arriving":
        policy["late_arriving"]["params"]["delay"] = {
            "family": "lognormal",
            "median": "PT30M",
            "p95": "PT2H",
        }
    return policy


def _realized_rate(proj: Projection, mode: str, n: int) -> float:
    """Realized rate = injections of ``mode`` / canonical events (CR-3)."""
    return len(proj.records_for(mode)) / n


def _run_stat_c(mode: str, n: int) -> None:
    """The shared STAT-C body for one mode: realized rate within ±1 pp of 5 %."""
    proj = run_projection(_single_mode_policy(mode), n=n)  # type: ignore[arg-type]
    realized = _realized_rate(proj, mode, n)
    assert abs(realized - RATE) <= ABS_TOL, (
        f"STAT-C {mode}: realized {realized:.4f} outside 5 % ± 1 % over {n} events"
    )


# --- PR-smoke (10k) --------------------------------------------------------


@pytest.mark.stat
@pytest.mark.parametrize("mode", _MODES)
def test_stat_c_per_mode_rate_smoke(mode: str) -> None:
    """Each mode at 5 % realizes 5 % ± 1 % over 10k events (smoke; STAT-C1..C7)."""
    _run_stat_c(mode, SMOKE_N)


@pytest.mark.stat
def test_stat_c2_late_honors_occurred_before_emitted_smoke() -> None:
    """Every late instance: occurred_at unchanged and delivered emitted_at later."""
    proj = run_projection(_single_mode_policy("late_arriving"), n=SMOKE_N)  # type: ignore[arg-type]
    assert proj.late_entries, "no late instances extracted at rate 0.05"
    for entry in proj.late_entries:
        env = entry["envelope"]
        # occurred_at is the canonical event time, never moved (INV-CHA-6).
        assert env["occurred_at"] == env["emitted_at"]  # fixture: canonical equal
        # due_at (the delivered emitted_at) is strictly after the canonical emitted_at.
        assert entry["due_at"] > env["emitted_at"]


# --- Nightly / Phase-9 gate (50k) ------------------------------------------


@pytest.mark.stat_nightly
@pytest.mark.parametrize("mode", _MODES)
def test_stat_c_per_mode_rate_gate(mode: str) -> None:
    """The binding gate: each mode at 5 % realizes 5 % ± 1 % over 50,000 events."""
    _run_stat_c(mode, GATE_N)


@pytest.mark.stat_nightly
def test_stat_c2_late_delay_median_within_15pct_gate() -> None:
    """Realized simulated-delay median within ±15 % of the configured 30 min.

    A 5 % selection rate over the 50k gate batch yields ~2.5k late selections —
    an ample sample for a stable median (the binding STAT-C check). Reaching 10k
    selections would need ~200k events; the median, not the count, is the gate.
    """
    proj = run_projection(_single_mode_policy("late_arriving"), n=GATE_N)  # type: ignore[arg-type]
    delays = [
        int(cast(int, r["details"]["delay_simulated_ms"]))
        for r in proj.records_for("late_arriving")
    ]
    assert len(delays) >= 2_000, f"only {len(delays)} late selections (need >=2k)"
    median = statistics.median(delays)
    assert abs(median - _30M_MS) <= 0.15 * _30M_MS, (
        f"realized delay median {median} ms outside ±15 % of {_30M_MS} ms"
    )


@pytest.mark.stat_nightly
def test_stat_c2_wall_realization_correct_at_k60_gate() -> None:
    """At k=60, the wall delay (due_at minus canonical emitted_at) is median/60 (§3.4)."""
    proj = run_projection(
        _single_mode_policy("late_arriving"),  # type: ignore[arg-type]
        n=20_000,
        k=60.0,
    )
    from datetime import datetime

    def _parse(ts: str) -> datetime:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    walls = []
    for entry in proj.late_entries:
        env = entry["envelope"]
        walls.append((_parse(entry["due_at"]) - _parse(env["emitted_at"])).total_seconds())
    assert walls
    wall_median_s = statistics.median(walls)
    # 30 simulated min / 60 = 30 wall seconds, within ±15 % (event-model §3.4 table).
    assert abs(wall_median_s - 30.0) <= 0.15 * 30.0, (
        f"k=60 wall-delay median {wall_median_s}s outside ±15 % of 30s"
    )
