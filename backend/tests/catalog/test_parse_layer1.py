"""Parse-hardening (MAN-S001/2/3) and Layer-1 schema (MAN-S004) tests.

Asserts the {code, path, bound, actual} shape per the adversarial-corpus rule
(testing-strategy §16.3): one failing fixture per MAN-S code.
"""

from __future__ import annotations

import json

import pytest
import yaml

from dataforge_engine.manifest import (
    MAX_DOCUMENT_BYTES,
    MAX_NESTING_DEPTH,
    ManifestParseError,
    parse_manifest_text,
    validate_manifest,
)

from .fixtures import valid_subset_manifest


def test_valid_subset_manifest_passes() -> None:
    report = validate_manifest(valid_subset_manifest())
    assert report.passed, report.codes()
    assert report.status == "passed"
    assert report.schema_version == "v0"
    assert report.errors == ()


def test_valid_manifest_parses_from_yaml_text() -> None:
    # sort_keys=False preserves declaration order, which the seed-order DAG check
    # (MAN-V111) depends on — the canonical JSON form the catalog stores preserves
    # order too (JSON object order is insertion order from the parsed document).
    text = yaml.safe_dump(valid_subset_manifest(), sort_keys=False)
    report = validate_manifest(text)
    assert report.passed, report.codes()


def test_valid_manifest_parses_from_json_text() -> None:
    text = json.dumps(valid_subset_manifest())
    report = validate_manifest(text)
    assert report.passed, report.codes()


# --- MAN-S001: anchors/aliases ------------------------------------------------


def test_man_s001_alias_rejected() -> None:
    text = "a: &x [1, 2, 3]\nb: *x\n"
    with pytest.raises(ManifestParseError) as exc:
        parse_manifest_text(text)
    assert exc.value.error.code == "MAN-S001"
    assert exc.value.error.path == ""


def test_man_s001_unused_anchor_rejected() -> None:
    """An anchor declared but never aliased is still rejected (defence in depth)."""
    with pytest.raises(ManifestParseError) as exc:
        parse_manifest_text("a: &x [1, 2, 3]\n")
    assert exc.value.error.code == "MAN-S001"


def test_man_s001_surfaces_as_failed_report() -> None:
    report = validate_manifest("a: &x 1\nb: *x\n")
    assert report.status == "failed"
    assert report.errors[0].code == "MAN-S001"


# --- MAN-S002: size -----------------------------------------------------------


def test_man_s002_oversize_document() -> None:
    text = "manifest_schema: v0\nbig: " + ("z" * (MAX_DOCUMENT_BYTES + 10))
    report = validate_manifest(text)
    err = report.errors[0]
    assert err.code == "MAN-S002"
    assert err.bound == MAX_DOCUMENT_BYTES
    assert isinstance(err.actual, int) and err.actual > MAX_DOCUMENT_BYTES


# --- MAN-S003: depth ----------------------------------------------------------


def test_man_s003_too_deep() -> None:
    node: dict[str, object] = {}
    cursor = node
    for _ in range(MAX_NESTING_DEPTH + 5):
        child: dict[str, object] = {}
        cursor["k"] = child
        cursor = child
    report = validate_manifest(yaml.safe_dump(node))
    err = report.errors[0]
    assert err.code == "MAN-S003"
    assert err.bound == MAX_NESTING_DEPTH
    assert isinstance(err.actual, int) and err.actual > MAX_NESTING_DEPTH


# --- MAN-S004: Layer-1 schema conformance -------------------------------------


def test_man_s004_missing_required_section() -> None:
    doc = valid_subset_manifest()
    del doc["seeding"]
    report = validate_manifest(doc)
    assert report.status == "failed"
    codes = report.codes()
    assert "MAN-S004" in codes


def test_man_s004_bad_manifest_schema_const() -> None:
    doc = valid_subset_manifest()
    doc["manifest_schema"] = "v1"
    report = validate_manifest(doc)
    s004 = [e for e in report.errors if e.code == "MAN-S004"]
    assert s004
    assert any(e.path == "/manifest_schema" for e in s004)


def test_man_s004_additional_property_closed_document() -> None:
    doc = valid_subset_manifest()
    doc["unexpected_top_level"] = True
    report = validate_manifest(doc)
    assert "MAN-S004" in report.codes()


def test_man_s004_has_json_pointer_path() -> None:
    doc = valid_subset_manifest()
    doc["entities"]["users"]["key_prefix"] = "X"  # violates ^[a-z]{2,8}$
    report = validate_manifest(doc)
    s004 = [e for e in report.errors if e.code == "MAN-S004"]
    assert any(e.path == "/entities/users/key_prefix" for e in s004)


def test_non_mapping_top_level_is_s004() -> None:
    report = validate_manifest("- 1\n- 2\n")
    assert report.errors[0].code == "MAN-S004"
