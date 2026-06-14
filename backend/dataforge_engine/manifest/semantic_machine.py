"""Layer-2 probability / machine-structure checks (MAN-V201…V211, §8.2).

These checks operate on each state machine's graph (states as nodes, transitions
+ timeout edges as edges) and the per-state outgoing probability vectors:

* V201 probability-sum ≤ 1 + 1e-9 (remainder rule, §6.2);
* V202 remainder declared on a fully-allocated state;
* V203 terminal state carrying transitions/timeout/remainder;
* V204 orphan/unreachable state;
* V205 escape-less reachable SCC (Tarjan + absorption reachability);
* V206 fully-guarded state lacking ``remainder: exit``;
* V207 expected-steps ≤ 1000 via the absorbing-chain fundamental matrix;
* V208 each ``p_i ∈ (0, 1]`` and within override bounds;
* V209 non-terminal state with no transitions and no timeout;
* V210/V211 exactly one session machine binding the actor entity.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCollector, json_pointer
from .linalg import SingularMatrixError, expected_steps_to_absorption
from .model import ManifestView

# §6.2 rule 1 tolerance and B-13 expected-steps bound.
PROB_SUM_TOLERANCE = 1e-9
EXPECTED_STEPS_BOUND = 1000


def check_machines(view: ManifestView, errors: ErrorCollector) -> None:
    _check_session_cardinality(view, errors)
    for mname, machine in view.state_machines.items():
        _check_one_machine(view, mname, machine, errors)


def _check_session_cardinality(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V210/V211: exactly one session machine; it binds the actor entity."""
    sessions = [
        (name, m)
        for name, m in view.state_machines.items()
        if m.get("type") == "session"
    ]
    if len(sessions) != 1:
        errors.add(
            "MAN-V210",
            json_pointer("state_machines"),
            "manifest must declare exactly one session machine",
            bound=1,
            actual=len(sessions),
        )
    for name, machine in sessions:
        if machine.get("binds") != view.actor_entity:
            errors.add(
                "MAN-V211",
                json_pointer("state_machines", name, "binds"),
                "session machine must bind metadata.actor_entity",
                actual=machine.get("binds"),
            )


def _state_base(mname: str, sname: str) -> str:
    return json_pointer("state_machines", mname, "states", sname)


def _check_one_machine(
    view: ManifestView, mname: str, machine: dict[str, Any], errors: ErrorCollector
) -> None:
    states: dict[str, dict[str, Any]] = machine.get("states", {})
    initial = machine.get("initial", "")
    is_session = machine.get("type") == "session"

    # Per-state shape checks (V201/V202/V203/V206/V208/V209) and the missing-target
    # / probability vectors used by reachability + expected-steps.
    for sname, state in states.items():
        _check_state_shape(view, mname, sname, state, states, errors)

    reachable = _reachable_states(states, initial)
    _check_orphans(mname, states, reachable, errors)
    _check_escape_and_expected_steps(
        mname, states, initial, reachable, is_session, errors
    )


def _check_state_shape(
    view: ManifestView,
    mname: str,
    sname: str,
    state: dict[str, Any],
    states: dict[str, dict[str, Any]],
    errors: ErrorCollector,
) -> None:
    base = _state_base(mname, sname)
    terminal = bool(state.get("terminal", False))
    transitions = state.get("transitions", []) or []
    timeout = state.get("timeout")
    remainder = state.get("remainder")

    if terminal:
        if transitions or timeout is not None or remainder is not None:
            errors.add(
                "MAN-V203",
                base,
                "terminal state must have no transitions, timeout, or remainder",
                actual=sname,
            )
        return

    # Non-terminal: V209 — must have ≥1 transition or a timeout.
    if not transitions and timeout is None:
        errors.add(
            "MAN-V209",
            base,
            "non-terminal state has no transitions and no timeout",
            actual=sname,
        )

    # V208 — each p_i ∈ (0,1] (schema enforces the range; re-check + override bounds).
    prob_sum = 0.0
    all_guarded = bool(transitions)
    for tidx, transition in enumerate(transitions):
        prob = float(transition.get("probability", 0.0))
        prob_sum += prob
        tbase = base + f"/transitions/{tidx}"
        if not (0.0 < prob <= 1.0):
            errors.add(
                "MAN-V208",
                tbase + "/probability",
                "transition probability must be in (0, 1]",
                actual=prob,
            )
        override = transition.get("override")
        if isinstance(override, dict):
            lo = override.get("min", 0.0)
            hi = override.get("max", 1.0)
            if not (lo <= prob <= hi):
                errors.add(
                    "MAN-V208",
                    tbase + "/probability",
                    "transition probability is outside its declared override bounds",
                    bound=[lo, hi],  # type: ignore[arg-type]
                    actual=prob,
                )
        if transition.get("guard") is None:
            all_guarded = False
        # V107 target existence handled here as a structural V204 prerequisite.
        if transition.get("to") not in states:
            errors.add(
                "MAN-V204",
                tbase + "/to",
                "transition targets a state that does not exist",
                actual=transition.get("to"),
            )
    if timeout is not None and timeout.get("to") not in states:
        errors.add(
            "MAN-V204",
            base + "/timeout/to",
            "timeout targets a state that does not exist",
            actual=timeout.get("to"),
        )

    # V201 — sum rule.
    if prob_sum > 1.0 + PROB_SUM_TOLERANCE:
        errors.add(
            "MAN-V201",
            base,
            f"outgoing probabilities sum to {prob_sum:g}; must be <= 1.0",
            bound=1.0,
            actual=round(prob_sum, 12),
        )

    # V202 — remainder declared on a fully-allocated (S == 1.0) state.
    fully_allocated = abs(prob_sum - 1.0) <= PROB_SUM_TOLERANCE
    if remainder is not None and fully_allocated:
        errors.add(
            "MAN-V202",
            base + "/remainder",
            "remainder declared on a fully-allocated state (sum == 1.0)",
            actual=remainder,
        )

    # V206 — every outgoing transition guarded ⇒ must declare remainder: exit.
    if all_guarded and remainder != "exit":
        errors.add(
            "MAN-V206",
            base,
            "fully-guarded state must declare remainder: exit",
            actual=remainder,
        )


def _reachable_states(
    states: dict[str, dict[str, Any]], initial: str
) -> set[str]:
    """States reachable from ``initial`` over transition + timeout edges (V204)."""
    if initial not in states:
        return set()
    seen: set[str] = set()
    stack = [initial]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in _out_edges(states.get(node, {})):
            if nxt in states and nxt not in seen:
                stack.append(nxt)
    return seen


def _out_edges(state: dict[str, Any]) -> list[str]:
    edges: list[str] = []
    for transition in state.get("transitions", []) or []:
        to = transition.get("to")
        if isinstance(to, str):
            edges.append(to)
    timeout = state.get("timeout")
    if isinstance(timeout, dict) and isinstance(timeout.get("to"), str):
        edges.append(timeout["to"])
    return edges


def _check_orphans(
    mname: str,
    states: dict[str, dict[str, Any]],
    reachable: set[str],
    errors: ErrorCollector,
) -> None:
    """MAN-V204: states unreachable from ``initial``."""
    for sname in states:
        if sname not in reachable:
            errors.add(
                "MAN-V204",
                _state_base(mname, sname),
                "state is unreachable from the initial state",
                actual=sname,
            )


def _is_graph_absorbing(state: dict[str, Any]) -> bool:
    """A state that ends a traversal *by its own shape* — ignoring session timeout.

    ``terminal: true`` or a non-terminal ``remainder: exit`` (its remainder mass
    leaves the machine). This is the absorption notion the expected-steps matrix
    (V207) uses: the implicit ``session_timeout`` is a wall-clock backstop, not a
    graph edge, so V207 must still catch near-absorbing ``stay`` loops in session
    machines (stay p≈1 ⇒ expected≈1000).
    """
    if bool(state.get("terminal", False)):
        return True
    return state.get("remainder") == "exit"


def _is_absorbing(state: dict[str, Any], is_session: bool) -> bool:
    """A state is absorbing for V205 reachability (§8.2 V205).

    Adds the implicit ``session_timeout`` absorption for session machines (§6.1)
    to graph absorption: every session state is timeout-absorbable, so a session
    machine never trips the escape-less-SCC check — its livelock risk is caught by
    V207's expected-steps bound instead.
    """
    if is_session:
        return True
    return _is_graph_absorbing(state)


def _can_reach_absorption(
    states: dict[str, dict[str, Any]],
    reachable: set[str],
    is_session: bool,
) -> set[str]:
    """States (within ``reachable``) that can reach an absorbing state.

    Reverse BFS from the absorbing set over the transition/timeout edges; a state
    not in the result is in (or leads only into) an escape-less component (V205).
    """
    absorbing = {
        s for s in reachable if _is_absorbing(states.get(s, {}), is_session)
    }
    # Build reverse adjacency over reachable states.
    rev: dict[str, list[str]] = {s: [] for s in reachable}
    for s in reachable:
        for nxt in _out_edges(states.get(s, {})):
            if nxt in reachable:
                rev[nxt].append(s)
    can: set[str] = set(absorbing)
    stack = list(absorbing)
    while stack:
        node = stack.pop()
        for pred in rev.get(node, []):
            if pred not in can:
                can.add(pred)
                stack.append(pred)
    return can


def _check_escape_and_expected_steps(
    mname: str,
    states: dict[str, dict[str, Any]],
    initial: str,
    reachable: set[str],
    is_session: bool,
    errors: ErrorCollector,
) -> None:
    """MAN-V205 (escape-less SCC) and MAN-V207 (expected-steps via fundamental matrix)."""
    if initial not in states or not reachable:
        return

    can_absorb = _can_reach_absorption(states, reachable, is_session)
    escape_less = sorted(reachable - can_absorb)
    if escape_less:
        for sname in escape_less:
            errors.add(
                "MAN-V205",
                _state_base(mname, sname),
                "state belongs to a reachable component with no path to absorption",
                actual=sname,
            )
        return  # expected-steps is undefined on a non-absorbing chain

    # MAN-V207 — expected transitions from initial to absorption. Absorption here
    # is *graph* absorption (terminal / exit-remainder); the session_timeout
    # backstop is excluded so near-absorbing stay-loops are still caught.
    transient = [s for s in reachable if not _is_graph_absorbing(states.get(s, {}))]
    if not transient:
        return  # initial is itself graph-absorbing
    # Put the initial state first so expected_steps returns t[initial].
    if initial in transient:
        transient.remove(initial)
        transient.insert(0, initial)
    index = {s: i for i, s in enumerate(transient)}
    n = len(transient)
    sub_q = [[0.0] * n for _ in range(n)]
    for s in transient:
        _fill_q_row(states[s], index, index[s], sub_q[index[s]])

    try:
        expected = expected_steps_to_absorption(transient, sub_q)
    except SingularMatrixError:
        # A singular (I - Q) means the transient block never graph-absorbs. For a
        # lifecycle machine that is a guaranteed-infinite component (§8.2 V207
        # note: "reported as V205"). For a session machine the session_timeout
        # backstop absorbs, so V205 is exempt — but the expected-steps in the
        # configured-rate model is unbounded, which V207 flags (over the bound).
        code = "MAN-V207" if is_session else "MAN-V205"
        message = (
            "expected transitions to absorption is unbounded (> 1000) under the "
            "configured rates"
            if is_session
            else "machine has a near-singular fundamental matrix (no guaranteed absorption)"
        )
        errors.add(
            code,
            json_pointer("state_machines", mname),
            message,
            bound=EXPECTED_STEPS_BOUND if is_session else None,
            actual=mname if not is_session else None,
        )
        return
    if expected > EXPECTED_STEPS_BOUND:
        errors.add(
            "MAN-V207",
            json_pointer("state_machines", mname),
            f"expected transitions to absorption is {expected:.1f}; must be <= 1000",
            bound=EXPECTED_STEPS_BOUND,
            actual=round(expected, 3),
        )


def _fill_q_row(
    state: dict[str, Any], index: dict[str, int], self_idx: int, row: list[float]
) -> None:
    """Populate one ``Q`` row: probability mass to each transient state.

    Guards are treated as passing (§8.2 V207). A ``stay`` remainder is a self-loop
    (its remainder mass re-enters this state); an ``exit`` remainder's mass leaves
    to absorption and is not recorded in ``Q``. Timeout edges carry no probability
    in the configured-rate model, so they are excluded from the expected-rate
    matrix (consistent with V207 using configured transition probabilities).
    """
    prob_sum = 0.0
    for transition in state.get("transitions", []) or []:
        to = transition.get("to")
        prob = float(transition.get("probability", 0.0))
        prob_sum += prob
        if to in index:
            row[index[to]] += prob
    leftover = max(0.0, 1.0 - prob_sum)
    if state.get("remainder") == "stay" and leftover > 0.0:
        # self-loop: re-enter this state with the remainder mass.
        row[self_idx] += leftover
