"""GOLD-A — byte-identical batch under the deterministic injected wall clock.

The host-side slice of GOLD-A (testing-strategy §6): driving the engine through
the dataset driver with a :class:`~generation.infra.clock.DeterministicWallClock`
makes the **full** envelope — including the wall ``emitted_at`` field — byte-stable
across runs at a fixed seed + pinned virtual epoch. On a mismatch the first
divergent line is reported. Pure engine + ports; no DB needed for the identity
check itself, so this runs in the unit lane under the ``golden`` marker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dataforge_engine.envelope import canonical_serialize
from generation.application.engine_driver import build_shard
from generation.infra.clock import DeterministicWallClock
from tests.generation.conftest import WorkspaceFixture


def _golden_run(gen_workspace: WorkspaceFixture, seed: int) -> list[bytes]:
    """Run a bounded batch with a pinned wall clock; return canonical envelope bytes.

    Uses ``build_shard`` + ``run_batch`` with a no-op ledger (in-memory) so the
    identity check is over the produced envelopes — the full internal envelope incl.
    the wall ``emitted_at`` stamped from the deterministic clock.
    """
    from generation.application.services import _plan_for

    plan = _plan_for(
        instance=gen_workspace.instance,
        workspace_id=str(gen_workspace.workspace.id),
        stream_id="00000000-0000-0000-0000-000000000001",
        seed=seed,
        simulated_days=1,
        virtual_epoch=datetime(2026, 1, 1, tzinfo=UTC),
    )
    shard = build_shard(plan, DeterministicWallClock(epoch=datetime(2026, 6, 13, tzinfo=UTC)))
    produced = shard.run_batch(max_events=300, until_us=plan.until_us)
    return [canonical_serialize(e) for e in produced]


@pytest.mark.golden
@pytest.mark.django_db
def test_gold_a_byte_identity(gen_workspace: WorkspaceFixture) -> None:
    """Same seed + pinned epoch + deterministic wall clock → byte-identical batch."""
    a = _golden_run(gen_workspace, seed=42)
    b = _golden_run(gen_workspace, seed=42)
    assert a, "the golden batch produced no events"
    if a != b:
        first = next(i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y)
        raise AssertionError(
            f"GOLD-A divergence at line {first}:\n  run-1: {a[first]!r}\n  run-2: {b[first]!r}"
        )


@pytest.mark.golden
@pytest.mark.django_db
def test_gold_a_different_seed_diverges(gen_workspace: WorkspaceFixture) -> None:
    """A different seed produces a different canonical batch (no accidental fixity)."""
    a = _golden_run(gen_workspace, seed=42)
    c = _golden_run(gen_workspace, seed=43)
    assert a != c
