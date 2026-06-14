"""Layer-2 machine-structure tests (MAN-V201…V211).

Includes the three demo cases the phase doc calls out: probability-sum (V201),
escape-less strongly-connected component (V205), and expected-steps (V207) via the
absorbing-Markov fundamental matrix.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import validate_manifest

from .fixtures import valid_subset_manifest


def _lifecycle_machine(states: dict[str, Any], initial: str = "a") -> dict[str, Any]:
    """Replace the lifecycle machine with a custom state graph (binds orders)."""
    doc = valid_subset_manifest()
    doc["state_machines"]["order_lifecycle"] = {
        "type": "lifecycle",
        "binds": "orders",
        "initial": initial,
        "states": states,
    }
    return doc


def test_man_v201_probability_sum_exceeds_one() -> None:
    """Demo case: outgoing probabilities sum > 1.0 → MAN-V201 with bound/actual."""
    doc = valid_subset_manifest()
    state = doc["state_machines"]["shopping_session"]["states"]["checkout"]
    state["transitions"] = [
        {"to": "ordered", "probability": 0.70, "emit": "order_placed"},
        {"to": "ordered", "probability": 0.45},
    ]
    state.pop("remainder", None)
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V201"]
    assert errs, report.codes()
    err = errs[0]
    assert err.path == "/state_machines/shopping_session/states/checkout"
    assert err.bound == 1.0
    assert isinstance(err.actual, float) and err.actual > 1.0


def test_man_v202_remainder_on_fully_allocated_state() -> None:
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 1.0}],
            },
            "b": {"terminal": True},
        }
    )
    assert "MAN-V202" in validate_manifest(doc).codes()


def test_man_v203_terminal_state_with_transitions() -> None:
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 0.5}],
            },
            "b": {"terminal": True, "transitions": [{"to": "a", "probability": 0.5}]},
        }
    )
    assert "MAN-V203" in validate_manifest(doc).codes()


def test_man_v204_orphan_state() -> None:
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 0.5}],
            },
            "b": {"terminal": True},
            "island": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 0.5}],
            },
        }
    )
    errs = [e for e in validate_manifest(doc).errors if e.code == "MAN-V204"]
    assert any(e.actual == "island" for e in errs)


def test_man_v205_escape_less_scc() -> None:
    """Demo case: a reachable SCC with no path to absorption → MAN-V205.

    ``a ⇄ b`` form a cycle; neither is terminal nor exit-remainder, and this is a
    lifecycle machine (no session_timeout backstop), so absorption is unreachable.
    """
    doc = _lifecycle_machine(
        {
            "a": {"transitions": [{"to": "b", "probability": 1.0}]},
            "b": {"transitions": [{"to": "a", "probability": 1.0}]},
        }
    )
    errs = [e for e in validate_manifest(doc).errors if e.code == "MAN-V205"]
    assert errs, validate_manifest(doc).codes()
    assert {e.actual for e in errs} >= {"a", "b"}


def test_man_v206_fully_guarded_state_without_exit_remainder() -> None:
    doc = _lifecycle_machine(
        {
            "a": {
                "transitions": [
                    {
                        "to": "b",
                        "probability": 0.9,
                        "guard": {
                            "all": [
                                {"path": "subject.item_count", "op": "gte", "value": 1}
                            ]
                        },
                    }
                ]
            },
            "b": {"terminal": True},
        }
    )
    assert "MAN-V206" in validate_manifest(doc).codes()


def test_man_v207_expected_steps_exceeds_bound() -> None:
    """Demo case: a near-absorbing stay-loop (p≈0.999) → expected≈1000 > bound.

    State ``a`` keeps 0.9995 of its mass as a ``stay`` self-loop, transitioning out
    with p=0.0005 — expected steps ≈ 1/0.0005 = 2000 > 1000 → MAN-V207.
    """
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "stay",
                "transitions": [{"to": "b", "probability": 0.0005}],
            },
            "b": {"terminal": True},
        }
    )
    errs = [e for e in validate_manifest(doc).errors if e.code == "MAN-V207"]
    assert errs, validate_manifest(doc).codes()
    err = errs[0]
    assert err.bound == 1000
    assert isinstance(err.actual, float) and err.actual > 1000


def test_man_v207_session_loop_without_graph_absorption() -> None:
    """A session machine that only timeout-absorbs (closed loop) → V207, not V205.

    ``started -> looping -> started`` never graph-absorbs; the session_timeout
    backstop exempts it from the escape-less-SCC check (V205), but its
    configured-rate expected steps are unbounded, which V207 flags.
    """
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["states"] = {
        "started": {
            "transitions": [
                {"to": "looping", "probability": 1.0, "emit": "session_started"}
            ]
        },
        "looping": {"transitions": [{"to": "started", "probability": 1.0}]},
    }
    codes = set(validate_manifest(doc).codes())
    assert "MAN-V207" in codes
    assert "MAN-V205" not in codes


def test_man_v207_within_bound_passes() -> None:
    """A short absorbing chain is well under the expected-steps bound."""
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 0.5}],
            },
            "b": {"terminal": True},
        }
    )
    assert "MAN-V207" not in validate_manifest(doc).codes()


def test_man_v208_probability_outside_override_bounds() -> None:
    doc = valid_subset_manifest()
    transition = doc["state_machines"]["shopping_session"]["states"]["checkout"]["transitions"][0]
    transition["probability"] = 0.05  # below override.min == 0.10
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V208"]
    assert errs


def test_man_v209_non_terminal_dead_end() -> None:
    doc = _lifecycle_machine(
        {
            "a": {
                "remainder": "exit",
                "transitions": [{"to": "b", "probability": 0.5}],
            },
            "b": {},  # non-terminal, no transitions, no timeout
        }
    )
    errs = [e for e in validate_manifest(doc).errors if e.code == "MAN-V209"]
    assert any(e.actual == "b" for e in errs)


def test_man_v210_two_session_machines() -> None:
    doc = valid_subset_manifest()
    doc["state_machines"]["order_lifecycle"]["type"] = "session"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V210"]
    assert errs and errs[0].actual == 2


def test_man_v211_session_binds_non_actor() -> None:
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["binds"] = "orders"
    report = validate_manifest(doc)
    errs = [e for e in report.errors if e.code == "MAN-V211"]
    assert errs and errs[0].actual == "orders"
