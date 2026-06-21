"""CHD-7 — the 2^7 = 128 mode-toggle combination matrix (§10.3, nightly only).

Phase-9 exit criterion #3 (nightly + gate run): all 128 enable-combinations of the
seven modes run crash-free with full CHD-4 reconciliation per combination. This is
deselected on the PR lane (``chaos_nightly`` marker — 128 x 5k-event projections is
too slow for every PR); the PR subset is the 7 single-mode runs plus all-on, which
ride ``test_chd_reconciliation.py`` / ``test_stat_chaos.py`` on the fast lane.

Each combination runs the pure pipeline over a 5k canonical batch at rate 0.10 and
asserts: (1) it does not raise; (2) the ledger hash is unchanged (CHD-5); (3) every
enabled mode's injection count reconciles to the consumer-side delivered tally
(CHD-4); (4) disabled modes record nothing.
"""

from __future__ import annotations

import itertools

import pytest

from dataforge_engine.chaos import default_policy
from dataforge_engine.chaos.tests.fixtures import make_batch
from tests.chaos.projection import Projection, ledger_content_hash, run_projection

pytestmark = pytest.mark.chaos_nightly

N = 5000
RATE = 0.10
_MODES = (
    "missing",
    "duplicates",
    "corrupted_values",
    "nulls",
    "schema_drift",
    "out_of_order",
    "late_arriving",
)
_ALL_COMBOS = list(itertools.product((False, True), repeat=len(_MODES)))
assert len(_ALL_COMBOS) == 128


def _policy_for(combo: tuple[bool, ...]) -> object:
    policy = default_policy()
    for mode, enabled in zip(_MODES, combo, strict=True):
        policy[mode]["enabled"] = enabled  # type: ignore[literal-required]
        policy[mode]["rate"] = RATE  # type: ignore[literal-required]
    policy["late_arriving"]["params"]["delay"] = {"family": "fixed", "value": "PT30M"}
    return policy


def _reconcile(proj: Projection, combo: tuple[bool, ...]) -> None:
    enabled = dict(zip(_MODES, combo, strict=True))
    instances = proj.all_instance_dicts()
    for mode in _MODES:
        records = len(proj.records_for(mode))
        if not enabled[mode]:
            assert records == 0, f"{mode} disabled but recorded {records}"
            continue
        if mode == "missing":
            delivered_ids = {e["event_id"] for e in proj.delivered}
            buffered_ids = {x["event_id"] for x in proj.late_entries}
            ledger_ids = {e["event_id"] for e in proj.ledger}
            observed = len(ledger_ids - delivered_ids - buffered_ids)
        elif mode == "duplicates":
            observed = sum(
                1
                for e in instances
                if (e["_df"]["chaos"] or {}).get("duplicates", {}).get("duplicate_index", 0) >= 1
            )
        elif mode == "late_arriving":
            observed = len(proj.late_entries)
        elif mode in ("corrupted_values", "nulls", "schema_drift"):
            # value modes are keyed per canonical event (CR-1): distinct event_ids.
            observed = len({e["event_id"] for e in instances if mode in (e["_df"]["chaos"] or {})})
        else:  # out_of_order — per-instance (CR-2)
            observed = sum(1 for e in instances if mode in (e["_df"]["chaos"] or {}))
        assert observed == records, f"{mode}: observed {observed} != recorded {records}"


@pytest.mark.parametrize("combo", _ALL_COMBOS, ids=lambda c: "".join("1" if x else "0" for x in c))
def test_chd7_combination_crash_free_and_reconciled(combo: tuple[bool, ...]) -> None:
    """Every 2^7 combination runs crash-free with CHD-4/5 reconciliation."""
    proj = run_projection(_policy_for(combo), n=N)  # type: ignore[arg-type]
    # CHD-5: ledger untouched by ANY combination.
    assert ledger_content_hash(proj.ledger) == ledger_content_hash(make_batch(N))
    # CHD-4: per-mode reconciliation for the enabled modes; silence for disabled.
    _reconcile(proj, combo)
