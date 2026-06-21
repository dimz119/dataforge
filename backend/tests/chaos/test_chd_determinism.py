"""CHD-1/2/3 — chaos determinism on the deterministic projection (§10.1, INV-CHA-2).

Phase-9 exit criterion #2 (PR, permanent): identical ``(seed, chaos config)`` ⇒
identical injection sets on the deterministic projection. These run the PURE
:class:`ChaosPipeline` twice over the same canonical fixture and compare:

* **CHD-1:** the deterministic projection — ``(mode, event_id, sequence_no,
  duplicate_index, field-level details)`` — is equal across runs; wall-clock
  fields (``injection_id``, ``recorded_at``, ``due_at``, realized wall delay) are
  excluded (they are wall artifacts, not chaos decisions, §11).
* **CHD-2:** the delivered post-chaos sequence (event-id order incl. duplicate
  copies + the out-of-order shuffle) is identical across runs — the byte-identity
  the GOLD-C fixture pins permanently (``tests/golden/test_gold_c_replay.py``).
* **CHD-3:** the late-arrival schedule's simulated-delay assignments are identical
  across runs, and ``wall_delay = simulated_delay / k`` holds at k ∈ {1, 60}.

Pure engine + ports — no Postgres, no broker; the fast PR chaos lane.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

import pytest

from dataforge_engine.chaos import default_policy
from tests.chaos.projection import (
    all_modes_policy,
    deterministic_projection,
    run_projection,
)

pytestmark = pytest.mark.chaos

N = 5000


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_chd1_identical_seed_config_identical_projection() -> None:
    """CHD-1: two all-7-modes runs produce the identical deterministic projection."""
    a = run_projection(all_modes_policy(0.10), n=N)
    b = run_projection(all_modes_policy(0.10), n=N)
    proj_a = deterministic_projection(a.records)
    proj_b = deterministic_projection(b.records)
    assert proj_a == proj_b
    assert proj_a, "the projection is empty — no injections to compare"


def test_chd1_projection_excludes_wall_clock_fields() -> None:
    """CHD-1: injection_id / due_at_wall are wall artifacts, absent from the projection."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    rows = deterministic_projection(proj.records)
    flat = " ".join(row[-1] for row in rows)  # the JSON detail blob of each row
    assert "due_at_wall" not in flat
    assert "realized_wall_delay_ms" not in flat
    assert "injection_id" not in flat


def test_chd2_delivered_sequence_identical_across_runs() -> None:
    """CHD-2: the delivered event-id order (copies + shuffle) is identical."""
    a = run_projection(all_modes_policy(0.10), n=N)
    b = run_projection(all_modes_policy(0.10), n=N)
    seq_a = [(e["event_id"], e["sequence_no"], e["_df"]["canonical"]) for e in a.delivered]
    seq_b = [(e["event_id"], e["sequence_no"], e["_df"]["canonical"]) for e in b.delivered]
    assert seq_a == seq_b


def test_chd3_late_simulated_delays_identical_across_runs() -> None:
    """CHD-3: the late schedule's per-instance simulated-delay assignments are stable."""
    policy = default_policy()
    policy["late_arriving"]["enabled"] = True
    policy["late_arriving"]["rate"] = 0.10
    policy["late_arriving"]["params"]["delay"] = {
        "family": "lognormal",
        "median": "PT30M",
        "p95": "PT2H",
    }

    def _schedule() -> list[tuple[str, int]]:
        proj = run_projection(policy, n=N)
        return sorted(
            (r["event_id"], int(cast(int, r["details"]["delay_simulated_ms"])))
            for r in proj.records_for("late_arriving")
        )

    assert _schedule() == _schedule()


def test_chd3_wall_delay_is_simulated_over_k_at_k1_and_k60() -> None:
    """CHD-3: wall_delay = simulated_delay / k, asserted at k=1 and k=60 (§3.4)."""
    policy = default_policy()
    policy["late_arriving"]["enabled"] = True
    policy["late_arriving"]["rate"] = 0.10
    policy["late_arriving"]["params"]["delay"] = {"family": "fixed", "value": "PT30M"}

    for k, expected_wall_s in ((1.0, 1800.0), (60.0, 30.0)):
        proj = run_projection(policy, n=2000, k=k)
        assert proj.late_entries, f"no late selections at k={k}"
        for entry in proj.late_entries:
            emitted = _parse(entry["envelope"]["emitted_at"])
            wall = (_parse(entry["due_at"]) - emitted).total_seconds()
            assert wall == expected_wall_s, f"k={k}: wall_delay {wall}s != {expected_wall_s}s"
            # The simulated delay recorded on the entry is invariant to k.
            assert entry["delay_simulated_ms"] == 30 * 60 * 1000
