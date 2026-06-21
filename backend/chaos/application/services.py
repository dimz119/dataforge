"""Use-case services for the Chaos context.

Services own the transaction boundary and orchestrate domain models plus
infra adapters (backend-architecture §3.1, application layer).

The :func:`resolve_policy` helper turns a stream's live ``chaos_config`` document
(the desired-state value, §3.2) into a fully-populated engine
:class:`~dataforge_engine.chaos.ChaosPolicy` so the runner can build the pipeline
without reaching into engine internals — it merges the live document over the
disabled :func:`~dataforge_engine.chaos.default_policy` (absent keys fall back to
the defaults, so a partial document is still a valid, closed seven-mode policy).
"""

from __future__ import annotations

from typing import Any, cast

from dataforge_engine.chaos import CHAOS_MODES, ChaosPolicy, OnStopPolicy, default_policy

__all__ = ["resolve_on_stop_policy", "resolve_policy"]


def resolve_policy(chaos_config: dict[str, Any] | None) -> ChaosPolicy:
    """Merge a live ``chaos_config`` document over the disabled default policy.

    Mode-level merge (the API contract, §3.5): each present mode replaces the
    default entry wholesale; absent modes keep their disabled default. The result
    is always the closed seven-mode shape the pipeline expects.
    """
    policy = default_policy()
    if not chaos_config:
        return policy
    for mode in CHAOS_MODES:
        entry = chaos_config.get(mode)
        if isinstance(entry, dict):
            policy[mode] = cast(Any, entry)
    stop = chaos_config.get("on_stop_policy")
    if stop in ("discard", "flush"):
        policy["on_stop_policy"] = cast(OnStopPolicy, stop)
    return policy


def resolve_on_stop_policy(chaos_config: dict[str, Any] | None) -> OnStopPolicy:
    """The effective ``on_stop_policy`` (``discard`` default, §3.2/§6.3)."""
    if chaos_config:
        stop = chaos_config.get("on_stop_policy")
        if stop in ("discard", "flush"):
            return cast(OnStopPolicy, stop)
    return "discard"
