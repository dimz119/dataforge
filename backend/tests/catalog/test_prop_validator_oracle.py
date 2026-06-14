"""PROP-SM-2 — validator round-trip mutation oracle (testing-strategy §16.1, §17.1).

Mutate the valid base manifest with generated edits (probability perturbations,
deleted/added states, dangling refs, oversized durations, unknown generators) and
assert the validator's verdict matches an **independent oracle** classification:
every structurally invalid mutation yields the documented MAN-* code, and a valid
(identity / additive-but-legal) mutation still passes (TP-5 — one wall for every
manifest). The oracle is intentionally dumb and declarative — it predicts the code
from the *intent* of the edit, never by re-running the validator — so a divergence
catches either a validator regression or an oracle drift, not a tautology.

Hypothesis profiles per §17.1: ``ci`` (200 examples, derandomized) is the default
under CI; ``dev`` is fast for local iteration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dataforge_engine.manifest import validate_manifest
from tests.catalog.fixtures.base import valid_subset_manifest

settings.register_profile("dev", max_examples=50)
settings.register_profile(
    "ci", max_examples=200, derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("ci")


@dataclass(frozen=True)
class Mutation:
    """An edit to the valid base plus the oracle's predicted outcome.

    ``expected_code`` is ``None`` for a mutation the oracle classifies as *still
    valid* (the validator must keep passing); otherwise it is the MAN-* code the
    validator must emit.
    """

    name: str
    apply: Any  # Callable[[dict], None]
    expected_code: str | None


def _set_prob_sum_over_one(doc: dict[str, Any]) -> None:
    state = doc["state_machines"]["shopping_session"]["states"]["checkout"]
    state["transitions"] = [
        {"to": "ordered", "probability": 0.7, "emit": "order_placed"},
        {"to": "ordered", "probability": 0.6},
    ]
    state.pop("remainder", None)


def _delete_target_state(doc: dict[str, Any]) -> None:
    # Remove 'ordered' (a referenced terminal) → its referrer points nowhere.
    doc["state_machines"]["shopping_session"]["states"].pop("ordered")


def _dangling_actor(doc: dict[str, Any]) -> None:
    doc["metadata"]["actor_entity"] = "nonexistent"


def _dangling_emit(doc: dict[str, Any]) -> None:
    started = doc["state_machines"]["shopping_session"]["states"]["started"]
    started["transitions"][0]["emit"] = "gone"


def _unknown_generator(doc: dict[str, Any]) -> None:
    doc["entities"]["users"]["attributes"]["x"] = {"generator": "totally.invented"}


def _oversized_duration(doc: dict[str, Any]) -> None:
    doc["state_machines"]["shopping_session"]["session_timeout"] = "P900D"


def _escape_less_cycle(doc: dict[str, Any]) -> None:
    doc["state_machines"]["order_lifecycle"]["initial"] = "a"
    doc["state_machines"]["order_lifecycle"]["states"] = {
        "a": {"transitions": [{"to": "b", "probability": 1.0}]},
        "b": {"transitions": [{"to": "a", "probability": 1.0}]},
    }


def _add_title_annotation(doc: dict[str, Any]) -> None:
    # A pure annotation change — must still be valid (the identity-class control).
    doc["metadata"]["title"] = "A perfectly fine retitled manifest"


def _lower_a_probability(doc: dict[str, Any]) -> None:
    # Within bounds — still valid.
    txn = doc["state_machines"]["shopping_session"]["states"]["started"]["transitions"][0]
    txn["probability"] = 0.40


MUTATIONS: tuple[Mutation, ...] = (
    Mutation("prob_sum_over_one", _set_prob_sum_over_one, "MAN-V201"),
    Mutation("dangling_actor", _dangling_actor, "MAN-V101"),
    Mutation("dangling_emit", _dangling_emit, "MAN-V107"),
    Mutation("unknown_generator", _unknown_generator, "MAN-S004"),
    Mutation("oversized_duration", _oversized_duration, "MAN-V315"),
    Mutation("escape_less_cycle", _escape_less_cycle, "MAN-V205"),
    # Deleting a referenced state breaks reachability/closure; the validator must
    # reject it (orphan/dangling — the oracle only asserts "not passed").
    Mutation("delete_target_state", _delete_target_state, "INVALID"),
    # Valid-class controls — the validator must keep passing.
    Mutation("retitle", _add_title_annotation, None),
    Mutation("lower_probability", _lower_a_probability, None),
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mutation=st.sampled_from(MUTATIONS))
def test_validator_verdict_matches_oracle(mutation: Mutation) -> None:
    doc = valid_subset_manifest()
    mutation.apply(doc)
    report = validate_manifest(doc)

    if mutation.expected_code is None:
        assert report.passed, (
            f"{mutation.name}: oracle says valid but validator rejected with "
            f"{report.codes()}"
        )
    elif mutation.expected_code == "INVALID":
        assert not report.passed, f"{mutation.name}: oracle says invalid but validator passed"
    else:
        assert not report.passed, f"{mutation.name}: oracle expected {mutation.expected_code}"
        assert mutation.expected_code in report.codes(), (
            f"{mutation.name}: oracle expected {mutation.expected_code}; "
            f"validator emitted {report.codes()}"
        )


@given(
    p1=st.floats(min_value=0.01, max_value=0.99),
    p2=st.floats(min_value=0.01, max_value=0.99),
)
def test_probability_sum_oracle_over_arbitrary_pairs(p1: float, p2: float) -> None:
    """For any two outgoing probabilities, the validator agrees with arithmetic.

    ``p1 + p2 > 1.0 + 1e-9`` ⟺ MAN-V201. This is the §6.2 rule-1 oracle stated as
    pure arithmetic — Hypothesis sweeps the boundary the demo only spot-checks.
    """
    doc = valid_subset_manifest()
    state = doc["state_machines"]["shopping_session"]["states"]["checkout"]
    state["transitions"] = [
        {"to": "ordered", "probability": p1, "emit": "order_placed"},
        {"to": "ordered", "probability": p2},
    ]
    state.pop("remainder", None)
    codes = validate_manifest(doc).codes()
    oracle_violates = (p1 + p2) > 1.0 + 1e-9
    assert ("MAN-V201" in codes) == oracle_violates, (
        f"p1={p1} p2={p2} sum={p1 + p2}; oracle violates={oracle_violates}; codes={codes}"
    )
