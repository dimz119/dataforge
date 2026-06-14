"""Hardened manifest parse front-end (§8 parse hardening) and Layer-1 schema conformance.

The parse stage runs **before** any validation layer and is the first ring of the
untrusted-manifest defence (§13, T-6):

* YAML **safe** loader only — no arbitrary object construction;
* anchors / aliases rejected outright (billion-laughs / quadratic blow-up) → MAN-S001;
* raw document ≤ 512 KiB (B-01) → MAN-S002;
* nesting depth ≤ 12 (B-02) → MAN-S003;
* canonicalisation to a plain ``dict`` for hashing / storage / validation.

JSON input is accepted too (the catalog stores canonical JSON); a JSON document is
a strict subset of YAML, so the same safe-load path handles both. Anchors/aliases
are a YAML-only concern; JSON has none.

Layer 1 (§8.1) validates the parsed structure against the Manifest v0 JSON Schema
(§9.1) using Draft 2020-12, emitting one ``MAN-S004`` per failure with a JSON
Pointer path.

Pure Python (BE-ENG-1): PyYAML + jsonschema only, both framework-free.
"""

from __future__ import annotations

from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from .errors import ValidationError, json_pointer
from .schema_gen import generate_manifest_schema

# B-01 / B-02 (§8, §9.1 notes).
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_NESTING_DEPTH = 12


class ManifestParseError(Exception):
    """Parse-stage failure carrying its canonical :class:`ValidationError`.

    Raised when a document cannot even be turned into a structure to validate
    (MAN-S001/S002/S003) or is not valid YAML/JSON at all. The public
    ``validate_manifest`` catches it and returns a failed report; direct callers
    can inspect ``.error``.
    """

    def __init__(self, error: ValidationError) -> None:
        super().__init__(error.message)
        self.error = error


def _anchors_error() -> ManifestParseError:
    return ManifestParseError(
        ValidationError(
            code="MAN-S001",
            path="",
            message="YAML anchors/aliases are not permitted in manifests",
            scope="manifest",
        )
    )


def _reject_anchors_aliases(stream: str) -> None:
    """Scan the YAML event stream and reject any anchor (``&x``) or alias (``*x``).

    PyYAML clears a node's ``anchor`` after composing, so node-graph inspection is
    unreliable; the **event** stream exposes anchors (on the collection/scalar
    start event) and ``AliasEvent`` directly. An anchor declared but never aliased
    is still caught (defence in depth, T-6). The scan is linear in the event
    count, itself bounded by the already-checked B-01 document size. Construction
    never runs (so a billion-laughs alias graph is never expanded). JSON, which
    has no anchors, passes untouched.
    """
    for event in yaml.parse(stream, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            raise _anchors_error()
        if getattr(event, "anchor", None) is not None:
            raise _anchors_error()


def _compose_no_anchors(stream: str) -> Any:
    """Safe-load after rejecting anchors/aliases (MAN-S001), returning a plain object."""
    _reject_anchors_aliases(stream)
    return yaml.safe_load(stream)


def _document_depth(obj: Any, _depth: int = 1) -> int:
    """Maximum container-nesting depth of a parsed structure (MAN-S003 / B-02).

    B-02 counts **nesting levels** as the spec writes them: a path like
    ``state_machines.*.states.*.transitions[].effects[].set.*.generated.params.options[]``
    is "11 levels" (scenario-plugin-architecture §4.1 / ecommerce.md §4.1), where
    each ``[]`` array is one level — not two. So descending from a list into its
    items does **not** add a level (the items live at the list's own depth); only
    object-key nesting and the list container itself increment depth. This keeps the
    normative reference manifest (whose effects carry ``generated.params`` value
    sources) within the ``MAX_NESTING_DEPTH`` bound, while a pathological chain of
    nested objects still trips MAN-S003 (each object adds a level).
    """
    if isinstance(obj, dict):
        if not obj:
            return _depth
        return max(_document_depth(v, _depth + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return _depth
        # Array items sit at the list's level (B-02 counts ``[]`` as one level).
        return max(_document_depth(v, _depth) for v in obj)
    return _depth


def parse_manifest_text(text: str) -> dict[str, Any]:
    """Parse + harden a raw manifest document, returning a plain ``dict``.

    Raises :class:`ManifestParseError` (MAN-S001/S002/S003) on a hardening
    violation, malformed YAML/JSON, or a non-object top level.
    """
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > MAX_DOCUMENT_BYTES:
        raise ManifestParseError(
            ValidationError(
                code="MAN-S002",
                path="",
                message="manifest exceeds the maximum document size",
                bound=MAX_DOCUMENT_BYTES,
                actual=len(raw_bytes),
                scope="manifest",
            )
        )

    try:
        document = _compose_no_anchors(text)
    except ManifestParseError:
        raise
    except yaml.YAMLError as exc:
        raise ManifestParseError(
            ValidationError(
                code="MAN-S004",
                path="",
                message=f"manifest is not valid YAML/JSON: {_yaml_reason(exc)}",
                scope="manifest",
            )
        ) from exc

    if not isinstance(document, dict):
        raise ManifestParseError(
            ValidationError(
                code="MAN-S004",
                path="",
                message="manifest top level must be a mapping/object",
                scope="manifest",
            )
        )

    depth = _document_depth(document)
    if depth > MAX_NESTING_DEPTH:
        raise ManifestParseError(
            ValidationError(
                code="MAN-S003",
                path="",
                message="manifest nesting depth exceeds the maximum",
                bound=MAX_NESTING_DEPTH,
                actual=depth,
                scope="manifest",
            )
        )
    return document


def _yaml_reason(exc: yaml.YAMLError) -> str:
    """A short, content-free reason for a YAML error (no document echo, AI-2)."""
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem is not None:
        return exc.problem
    return "syntax error"


# A single shared, compiled Layer-1 validator (the §9.1 schema is immutable).
_L1_VALIDATOR = Draft202012Validator(generate_manifest_schema())


def layer1_errors(document: dict[str, Any]) -> list[ValidationError]:
    """Layer 1: conformance to the Manifest v0 JSON Schema (§8.1), as MAN-S004.

    Each Draft-2020-12 failure becomes one ``MAN-S004`` with a JSON Pointer path
    built from the failure's ``absolute_path``. Errors are sorted by path then
    message for a deterministic report.
    """
    raw: list[JsonSchemaError] = sorted(
        _L1_VALIDATOR.iter_errors(document),
        key=lambda e: (list(e.absolute_path), e.message),
    )
    out: list[ValidationError] = []
    for err in raw:
        out.append(
            ValidationError(
                code="MAN-S004",
                path=json_pointer(*err.absolute_path),
                message=_schema_message(err),
                scope="manifest",
            )
        )
    return out


def _schema_message(err: JsonSchemaError) -> str:
    """A bounded, content-light message for a schema failure (AI-2).

    Echoes the validator keyword and the failing instance only for short scalar
    instances; long/structured instances are summarised by type to avoid
    amplifying attacker-controlled content through the error channel.
    """
    keyword = err.validator
    instance = err.instance
    if isinstance(instance, (dict, list)) or (
        isinstance(instance, str) and len(instance) > 64
    ):
        return f"does not satisfy schema constraint '{keyword}'"
    return f"value {instance!r} does not satisfy schema constraint '{keyword}'"
