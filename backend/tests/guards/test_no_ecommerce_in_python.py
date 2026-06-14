"""GUARD — zero e-commerce logic in Python (Phase-3 hard rule; exit criterion #5).

The reference scenario is DATA (``backend/catalog/builtin/ecommerce/1.0.0.yaml``),
never code. The runtime is generic: no per-scenario interpreter, subclass, or
branch. This permanent guard greps every backend **runtime** ``*.py`` file for the
token ``ecommerce`` and fails if it appears — proving no e-commerce *logic* lives
in Python.

Two allowances are not "logic" and are excluded:

* the data tree ``catalog/builtin/`` (the YAML lives here);
* test code — any path with a ``/tests/`` segment or a ``test_*.py`` name — which
  legitimately uses ``ecommerce`` / ``order_placed`` as *sample data* to exercise
  the generic loader/derivation (e.g. the envelope test fixtures);
* spec-doc citations ``ecommerce.md`` in a comment (a reference to the spec, not
  e-commerce logic).

A new ``ecommerce``-mentioning runtime ``.py`` is a regression by construction —
the generic-runtime invariant is enforced, not remembered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.guards

# Backend root = the directory containing manage.py (two levels up from tests/guards).
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# The data tree where the scenario name legitimately appears.
_DATA_PREFIX = "catalog/builtin/"
# Spec-doc filename citation (a reference to ../scenarios/ecommerce.md), not logic.
_DOC_CITATION = re.compile(r"ecommerce\.md")
_TOKEN = re.compile(r"ecommerce", re.IGNORECASE)


def _is_test_path(rel: str) -> bool:
    """True for any test code (a ``/tests/`` segment or a ``test_*.py`` basename)."""
    return rel.startswith("tests/") or "/tests/" in rel or Path(rel).name.startswith("test_")


def _candidate_py_files() -> list[Path]:
    files: list[Path] = []
    for path in _BACKEND_ROOT.rglob("*.py"):
        rel = path.relative_to(_BACKEND_ROOT).as_posix()
        if rel.startswith((".venv/", "node_modules/")) or "__pycache__" in rel:
            continue
        if "/migrations/" in f"/{rel}":
            continue
        files.append(path)
    return files


def test_no_ecommerce_token_in_runtime_python() -> None:
    offenders: list[str] = []
    for path in _candidate_py_files():
        rel = path.relative_to(_BACKEND_ROOT).as_posix()
        if rel.startswith(_DATA_PREFIX) or _is_test_path(rel):
            continue
        # Strip spec-doc citations (``ecommerce.md``) before scanning — a comment
        # referencing the scenario spec is documentation, not logic.
        text = _DOC_CITATION.sub("", path.read_text(encoding="utf-8", errors="replace"))
        if _TOKEN.search(text):
            offenders.append(rel)
    assert not offenders, (
        "e-commerce logic must never live in Python (Phase-3 hard rule). "
        f"Token 'ecommerce' found in runtime code: {offenders}. The reference "
        "scenario is data (catalog/builtin/ecommerce/*.yaml); the runtime is generic."
    )
