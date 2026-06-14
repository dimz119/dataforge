"""Unit tests for the pure-Python linear solver backing MAN-V207."""

from __future__ import annotations

import pytest

from dataforge_engine.manifest.linalg import (
    SingularMatrixError,
    expected_steps_to_absorption,
    solve,
)


def test_solve_identity() -> None:
    x = solve([[1.0, 0.0], [0.0, 1.0]], [3.0, 4.0])
    assert x == [3.0, 4.0]


def test_solve_simple_system() -> None:
    # 2x + y = 5 ; x + 3y = 10  → x = 1, y = 3
    x = solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
    assert x[0] == pytest.approx(1.0)
    assert x[1] == pytest.approx(3.0)


def test_solve_singular_raises() -> None:
    with pytest.raises(SingularMatrixError):
        solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])


def test_expected_steps_geometric_self_loop() -> None:
    """A single transient state with stay-prob p has expected 1/(1-p) steps.

    Q = [[0.999]] ⇒ (I-Q) = [[0.001]] ⇒ t = 1000.
    """
    t0 = expected_steps_to_absorption(["a"], [[0.999]])
    assert t0 == pytest.approx(1000.0, rel=1e-6)


def test_expected_steps_two_state_chain() -> None:
    # a → b (p=1), b → absorb (p=1): expected from a = 2 steps.
    t0 = expected_steps_to_absorption(["a", "b"], [[0.0, 1.0], [0.0, 0.0]])
    assert t0 == pytest.approx(2.0)


def test_expected_steps_non_absorbing_is_singular() -> None:
    # a ⇄ b with full mass and no absorption ⇒ (I-Q) singular.
    with pytest.raises(SingularMatrixError):
        expected_steps_to_absorption(["a", "b"], [[0.0, 1.0], [1.0, 0.0]])
