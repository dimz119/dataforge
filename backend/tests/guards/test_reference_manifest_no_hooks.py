"""GUARD — the reference scenario uses zero hooks (P-4; exit criterion #5).

The shipped builtin manifests must contain no ``hook`` generator: the 41-generator
built-in vocabulary is sufficient for the reference scenario, and a workspace
manifest with a hook fails validation anyway (MAN-V404). This permanent assertion
parses every builtin YAML and fails if any ``generator: hook`` appears, and also
confirms each builtin still validates against L1+L2 (the data stays loadable).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.guards

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "catalog" / "builtin"


def _builtin_files() -> list[Path]:
    return sorted(_BUILTIN_DIR.glob("*/*.yaml"))


def _walk_for_hook(node: Any) -> bool:
    """True iff any nested dict declares ``generator: hook``."""
    if isinstance(node, dict):
        if node.get("generator") == "hook":
            return True
        return any(_walk_for_hook(v) for v in node.values())
    if isinstance(node, list):
        return any(_walk_for_hook(item) for item in node)
    return False


def test_at_least_one_builtin_exists() -> None:
    assert _builtin_files(), "no builtin manifests found under catalog/builtin/"


@pytest.mark.parametrize("path", _builtin_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_builtin_contains_no_hook_generator(path: Path) -> None:
    from dataforge_engine.manifest import parse_manifest_text

    document = parse_manifest_text(path.read_text(encoding="utf-8"))
    assert not _walk_for_hook(document), f"{path} contains a hook generator (P-4 violation)"


@pytest.mark.parametrize("path", _builtin_files(), ids=lambda p: p.parent.name + "/" + p.name)
def test_builtin_passes_layers_1_and_2(path: Path) -> None:
    from catalog.application.validation import validate_catalog_manifest
    from dataforge_engine.manifest import parse_manifest_text

    document = parse_manifest_text(path.read_text(encoding="utf-8"))
    report = validate_catalog_manifest(document, is_workspace_visibility=False)
    assert report.passed, f"{path} failed L1+L2: {[e.code for e in report.errors]}"
