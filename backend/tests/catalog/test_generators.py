"""Layer-2 generator allowlist / params tests (MAN-V401…V406).

Plus the closed-allowlist + catalog invariants the §9.1 enum and §4 catalogs pin.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import (
    GENERATOR_NAMES,
    validate_manifest,
)

from .fixtures import valid_subset_manifest


def _errs(doc: dict[str, Any], code: str) -> list[Any]:
    return [e for e in validate_manifest(doc).errors if e.code == code]


def test_generator_catalog_matches_schema_enum() -> None:
    from dataforge_engine.manifest import generate_manifest_schema

    schema = generate_manifest_schema()
    enum = schema["$defs"]["generatorSpec"]["properties"]["generator"]["enum"]
    assert list(GENERATOR_NAMES) == enum
    assert "hook" in GENERATOR_NAMES
    assert len(GENERATOR_NAMES) == 41  # 40 value builtins + hook


def test_man_v401_unknown_generator_via_semantic() -> None:
    """A generator outside the allowlist trips L1 (closed enum) → MAN-S004.

    The semantic MAN-V401 is the catalog-consulting re-assertion; an L1-valid but
    catalog-absent generator cannot occur (the enum is the catalog), so the
    unknown-generator path is exercised by feeding the L2 walk directly.
    """
    from dataforge_engine.manifest import ErrorCollector, ManifestView
    from dataforge_engine.manifest.semantic_generators import check_generators

    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["x"] = {"generator": "made.up"}
    view = ManifestView(doc)
    collector = ErrorCollector()
    check_generators(view, collector, is_workspace_visibility=False)
    assert any(e.code == "MAN-V401" and e.actual == "made.up" for e in collector.errors)


def test_man_v402_unknown_param() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["item_count"]["params"]["bogus"] = 1
    errs = _errs(doc, "MAN-V402")
    assert any(e.actual == "bogus" for e in errs)


def test_man_v402_param_out_of_range() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["item_count"] = {
        "generator": "number.zipf",
        "params": {"n": 1, "s": 5.0},  # s max is 2.0
    }
    errs = _errs(doc, "MAN-V402")
    assert any(e.bound == 2.0 and e.actual == 5.0 for e in errs)


def test_man_v402_missing_required_param() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["orders"]["attributes"]["item_count"] = {
        "generator": "number.int",
        "params": {"min": 0},  # missing required 'max'
    }
    errs = _errs(doc, "MAN-V402")
    assert errs


def test_man_v403_hook_name_not_registered() -> None:
    """A global-visibility manifest with an unregistered hook name → MAN-V403."""
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["risk"] = {
        "generator": "hook",
        "params": {"name": "risk_score"},
    }
    report = validate_manifest(doc, is_workspace_visibility=False)
    assert any(e.code == "MAN-V403" and e.actual == "risk_score" for e in report.errors)


def test_man_v403_passes_for_registered_hook() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["risk"] = {
        "generator": "hook",
        "params": {"name": "risk_score"},
    }
    report = validate_manifest(
        doc, is_workspace_visibility=False, registered_hooks=frozenset({"risk_score"})
    )
    assert "MAN-V403" not in report.codes()


def test_man_v404_hook_in_workspace_manifest() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["risk"] = {
        "generator": "hook",
        "params": {"name": "risk_score"},
    }
    report = validate_manifest(
        doc, is_workspace_visibility=True, registered_hooks=frozenset({"risk_score"})
    )
    assert "MAN-V404" in report.codes()


def test_man_v405_template_unknown_placeholder() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["handle"] = {
        "generator": "template",
        "params": {"pattern": "{full_name}-{not_a_sibling}"},
    }
    errs = _errs(doc, "MAN-V405")
    assert any(e.actual == "not_a_sibling" for e in errs)


def test_man_v405_template_random_token_ok() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["attributes"]["handle"] = {
        "generator": "template",
        "params": {"pattern": "{full_name}-{#hex8}"},
    }
    assert "MAN-V405" not in validate_manifest(doc).codes()


def test_man_v406_expression_illegal_token() -> None:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"]["total"] = {
        "generated": {
            "generator": "derived.expr",
            "params": {"expr": "session.cart[].price ** 2", "output": "decimal"},
        }
    }
    errs = _errs(doc, "MAN-V406")
    assert errs


def test_man_v406_valid_expression_passes() -> None:
    doc = valid_subset_manifest()
    doc["event_types"]["order_placed"]["payload"]["total"] = {
        "generated": {
            "generator": "derived.expr",
            "params": {
                "expr": "round(sum(session.cart[].price) + 4.99, 2)",
                "output": "decimal",
                "scale": 2,
            },
        }
    }
    assert "MAN-V406" not in validate_manifest(doc).codes()
