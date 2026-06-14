"""GUARD — the behaviour engine is a generic interpreter, zero scenario code (BE-T1).

The Phase-4 hard rule (behavior-engine §1; phase exit + testing-strategy §17.3
permanent set): ``dataforge_engine.behavior`` is a GENERIC runtime — it interprets
any compiled manifest IR and contains **no** scenario knowledge: no branch,
subclass, or string-match on a scenario slug or scenario-specific event type. The
*same* engine runs the runner worker, the L3 dry-run worker, and the golden replay.

This permanent pytest gate mirrors the CI grep (``.github/workflows/ci.yaml`` →
"Guard — no scenario slugs in the engine"): it scans every runtime ``*.py`` under
``dataforge_engine/behavior`` (excluding the engine's own tests, which legitimately
name a sample slug to exercise the loader) for a scenario slug or e-commerce
event-type as a **string literal** — the shape scenario control-flow branching
would take. A hit is a generic-runtime regression, caught by construction rather
than by reviewer memory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.guards

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_DIR = _BACKEND_ROOT / "dataforge_engine" / "behavior"

# Scenario slugs + scenario-specific event/entity tokens that must never appear as a
# string literal in the generic engine (a quoted token is how a slug/event branch
# would read). Generic structural tokens (``actor``, ``session``, ``subject``,
# ``created``) are legitimate manifest-path vocabulary and are NOT scenario code.
_FORBIDDEN = (
    "ecommerce",
    "purchase_funnel",
    "shopping_session",
    "order_placed",
    "payment_authorized",
    "user_registered",
    "checkout_started",
    "cart_item_added",
)
# A token appearing inside single/double quotes (a string literal) in engine code.
_LITERAL = re.compile(
    "|".join(rf"""['"]{re.escape(tok)}['"]""" for tok in _FORBIDDEN)
)


def _engine_runtime_files() -> list[Path]:
    """Every runtime ``*.py`` under the engine, excluding its own tests + caches."""
    files: list[Path] = []
    for path in _ENGINE_DIR.rglob("*.py"):
        rel = path.relative_to(_ENGINE_DIR).as_posix()
        if "__pycache__" in rel or rel.startswith("tests/") or "/tests/" in rel:
            continue
        files.append(path)
    return files


def test_engine_dir_exists() -> None:
    """Sanity: the guard is actually scanning the engine (not a moved path)."""
    assert _ENGINE_DIR.is_dir(), f"engine dir missing at {_ENGINE_DIR}"
    assert _engine_runtime_files(), "no engine runtime files found to scan"


def test_no_scenario_slug_literal_in_engine() -> None:
    """No scenario slug / scenario-specific event-type string literal in the engine."""
    offenders: list[tuple[str, int, str]] = []
    for path in _engine_runtime_files():
        rel = path.relative_to(_BACKEND_ROOT).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _LITERAL.search(line):
                offenders.append((rel, line_no, line.strip()))
    assert not offenders, (
        "the behaviour engine must contain zero scenario code (BE-T1). Scenario "
        "slug / event-type string literal found in generic engine runtime:\n  "
        + "\n  ".join(f"{rel}:{ln}: {src}" for rel, ln, src in offenders)
    )
