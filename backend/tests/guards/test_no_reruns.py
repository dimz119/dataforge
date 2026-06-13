"""GUARD — no retry-to-green on determinism-bearing markers (testing §5.4/§17.3).

TP-2: a "flaky" engine test is by definition a determinism regression — it is
investigated, never retried. So no rerun/flaky mechanism may apply to the
``statistical``, ``golden``, or ``chaos`` markers. This meta-test parses the
effective pytest configuration and fails if any rerun plugin is active or any
rerun option is set — the visible, reviewable guard §5.4 mandates.

It is intentionally strict for the whole backend suite: DataForge pins every
determinism-touching test's seed, so a rerun plugin has no legitimate use here.
Introducing one would be a deliberate, reviewable edit to this file.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.guards

# Plugins whose presence + activation would let a failing test be retried.
_FORBIDDEN_RERUN_PLUGINS = ("rerunfailures", "flaky")
_FORBIDDEN_INI_KEYS = ("reruns", "reruns_delay")
_FORBIDDEN_ADDOPTS_FLAGS = ("--reruns", "--only-rerun", "--force-flaky", "--no-flaky-report")
_PROTECTED_MARKERS = ("statistical", "golden", "chaos")


def test_no_rerun_plugin_active(pytestconfig: pytest.Config) -> None:
    """No rerun/flaky plugin is registered active in the running session."""
    pm = pytestconfig.pluginmanager
    active = [name for name in _FORBIDDEN_RERUN_PLUGINS if pm.has_plugin(name)]
    assert not active, (
        f"A rerun/flaky plugin is active ({active}); retry-to-green is forbidden "
        f"(testing-strategy §5.4). Determinism-bearing markers {_PROTECTED_MARKERS} "
        "must never be retried."
    )


def test_no_rerun_options_configured(pytestconfig: pytest.Config) -> None:
    """No rerun ini keys are set and no rerun flag appears in addopts."""
    for key in _FORBIDDEN_INI_KEYS:
        try:
            value = pytestconfig.getini(key)
        except (ValueError, KeyError):
            value = None
        assert not value, f"pytest ini '{key}={value}' enables reruns (forbidden, §5.4)."

    addopts = pytestconfig.getini("addopts")
    flat = " ".join(addopts) if isinstance(addopts, list | tuple) else str(addopts or "")
    offending = [flag for flag in _FORBIDDEN_ADDOPTS_FLAGS if flag in flat]
    assert not offending, f"addopts contains rerun flag(s) {offending} (forbidden, §5.4)."


def test_protected_markers_documented() -> None:
    """The protected-marker list this guard enforces is the §5.4 set."""
    assert set(_PROTECTED_MARKERS) == {"statistical", "golden", "chaos"}
