"""The optional interpreter observer seam (behavior-engine §1; plugin-arch §8.4).

A single, generic, opt-in hook the L3 dry-run host attaches to the interpreter to
record *realized behavior* — which transitions are selected, whether their guards
pass, and when session traversals complete — without any scenario knowledge and
without touching the hot path when unset.

Why this lives in the engine (not the host): the data the dry run needs (realized
per-transition rates and guard-starved transitions, W-D610) is observable only
from inside the §6.2 selection/guard steps. The observer carries machine/state
names and transition *indices* (IR coordinates), never scenario semantics, so the
engine stays generic (BE-T1). The default :class:`NullObserver` is a no-op; when no
observer is attached the interpreter never calls it, so production / golden replay
pay nothing and behave identically (determinism is unaffected — the observer makes
no draws and no mutations).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["NullObserver", "Observer"]

# The sentinel transition index used for "the remainder policy was selected"
# (u >= S in §6.2): a real transition index is >= 0.
REMAINDER_INDEX = -1


@runtime_checkable
class Observer(Protocol):
    """Records the interpreter's realized §6.2 decisions (dry-run instrumentation).

    Every callback is fire-and-forget: it must not raise, mutate engine state, or
    draw randomness (that would perturb determinism). The interpreter invokes these
    only when an observer is attached.
    """

    def on_select(self, machine: str, state: str, transition_index: int) -> None:
        """A selection draw chose ``transition_index`` (or ``REMAINDER_INDEX``)."""
        ...

    def on_guard(self, machine: str, state: str, transition_index: int, *, passed: bool) -> None:
        """The selected transition's guard was evaluated (``passed`` records BE-G2)."""
        ...

    def on_session_complete(self, traversal_id: str) -> None:
        """A ``session`` traversal ended (terminal / exit / timeout, BE-A5)."""
        ...


class NullObserver:
    """The default no-op observer; the interpreter never calls it when unattached."""

    def on_select(self, machine: str, state: str, transition_index: int) -> None:
        return

    def on_guard(self, machine: str, state: str, transition_index: int, *, passed: bool) -> None:
        return

    def on_session_complete(self, traversal_id: str) -> None:
        return
