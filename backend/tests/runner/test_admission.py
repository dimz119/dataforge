"""Admission control tests (backend-architecture §8.1).

A runner refuses a new lease when adding the candidate shard's ``target_tps``
would push the held shards' aggregate over ``RUNNER_EPS_BUDGET``, or when the held
shard count is at ``RUNNER_SHARD_CAPACITY`` — TPS-weighted placement, no scheduler.
These exercise the pure :class:`runner.supervisor.AdmissionControl` directly.
"""

from __future__ import annotations

from runner.leases import ShardKey
from runner.supervisor import AdmissionControl


def _shard(n: int) -> ShardKey:
    return ShardKey.of(f"00000000-0000-0000-0000-0000000000{n:02d}", 0)


def test_admits_within_budget_then_refuses_over_budget() -> None:
    ac = AdmissionControl(eps_budget=5000, shard_capacity=8)
    s1, s2 = _shard(1), _shard(2)

    assert ac.admits(s1, 3000) is True
    ac.register(s1, 3000)
    assert ac.held_tps == 3000

    # 3000 + 3000 = 6000 > 5000 budget → refused.
    assert ac.admits(s2, 3000) is False
    # 3000 + 2000 = 5000 == budget → admitted (the boundary is inclusive).
    assert ac.admits(s2, 2000) is True


def test_refuses_over_shard_capacity() -> None:
    ac = AdmissionControl(eps_budget=1_000_000, shard_capacity=2)
    s1, s2, s3 = _shard(1), _shard(2), _shard(3)
    ac.register(s1, 1)
    ac.register(s2, 1)

    # Budget has headroom, but the shard cap (2) is reached → refused.
    assert ac.held_shards == 2
    assert ac.admits(s3, 1) is False


def test_already_held_shard_is_readmitted_free() -> None:
    ac = AdmissionControl(eps_budget=100, shard_capacity=1)
    s1 = _shard(1)
    ac.register(s1, 100)

    # Re-admitting a shard we already hold never re-counts against budget/cap.
    assert ac.admits(s1, 100) is True


def test_release_frees_budget_and_capacity() -> None:
    ac = AdmissionControl(eps_budget=5000, shard_capacity=1)
    s1, s2 = _shard(1), _shard(2)
    ac.register(s1, 5000)
    assert ac.admits(s2, 1) is False  # cap + budget both saturated

    ac.release(s1)
    assert ac.held_shards == 0
    assert ac.held_tps == 0
    assert ac.admits(s2, 5000) is True  # headroom restored
