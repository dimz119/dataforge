"""Compile a scheduled mid-stream upgrade's added-field bindings for the runner
(schema-registry §10.4 step 2; reused by the drift menu §11).

The runner cutover (§10.4) extends the event type's payload resolver with the union
chain of fields added across the versions in ``(effective, target]`` — for a skip
(``1 → 3``) that is v2's adds followed by v3's adds, **in version-introduction
order** (§10.3 "the union of added fields and bindings of versions 2 and 3"). Each
added property carries an ``x-df-binding`` annotation (§5.2) — a manifest
``valueSource`` (``{"from"}`` / ``{"const"}`` / ``{"generated"}``) — which this
module compiles into the engine's :class:`~dataforge_engine.behavior.ir.ValueSource`
so the interpreter can resolve the field at emit time against the same R-EVT-3
binding context the manifest's own fields use.

Two ordered, pure helpers share :func:`registry.infra.diff.diff_range` so the field
*order* is the deterministic version-introduction order (a jsonb round-trip does not
preserve object key order, so the diff is the canonical order source, not the stored
documents):

* :func:`build_added_field_bindings` — ``(field_name, ValueSource)`` tuples for the
  cutover resolver extension (the engine ``added_bindings`` of a ``SchemaCutover``);
* :func:`build_added_field_menu` — ``{path, fragment}`` entries for the drift field
  menu (DR-1) — the ``registry_view`` snapshot the chaos stage reads (chaos-engine
  §5.5). The drift workstream consumes this shape; it carries the full fragment
  (drift type-synthesizes from it; it does not bind).

Pure logic (BE layering: ``infra`` may import ``dataforge_engine``).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.behavior.ir import ValueSource, compile_value_source
from registry.infra.diff import diff_range

__all__ = [
    "build_added_field_bindings",
    "build_added_field_menu",
]


def build_added_field_bindings(
    documents: list[dict[str, Any]],
) -> tuple[tuple[str, ValueSource], ...]:
    """The ordered cutover resolver extension for the version chain ``documents``.

    ``documents`` is the contiguous ascending list of version documents from the
    *effective* version (exclusive — it is the chain root the diff measures against)
    to the *target* version (inclusive): e.g. ``[v1, v2, v3]`` for a ``1 → 3`` cutover,
    where v1 is the effective document and v2/v3 are the upgrade targets. The per-step
    diff yields the added properties in version-introduction order; for each, the
    field's ``x-df-binding`` annotation (looked up by its JSON Pointer in the document
    that introduced it, the *last* of the pair) is compiled to a
    :class:`~dataforge_engine.behavior.ir.ValueSource`.

    A field with no ``x-df-binding`` is skipped — Flow 2 rejects such a version
    (REG-C007), so by the time a version is registered every added field carries one;
    the guard keeps a malformed/partial document from raising at cutover time (the
    binding was validated at registration, schema-registry §5.2).
    """
    bindings: list[tuple[str, ValueSource]] = []
    for index in range(1, len(documents)):
        introduced_in = documents[index]
        for added in diff_range([documents[index - 1], documents[index]]).added_fields:
            field_name = _leaf_name(added.path)
            binding = _binding_at(introduced_in, added.path)
            if binding is None:
                continue
            bindings.append((field_name, compile_value_source(binding)))
    return tuple(bindings)


def build_added_field_menu(
    effective_document: dict[str, Any], next_document: dict[str, Any]
) -> list[dict[str, Any]]:
    """The drift field menu (DR-1): ``{path, fragment}`` per field the *next* version
    adds over the *effective* version.

    Drift injects the **next** registered version only (the lowest > effective,
    schema-registry §10.6 "next, not latest"), type-synthesizing each field from its
    declared fragment (chaos-engine §5.5 — drift is post-ledger and cannot read pools,
    so it does not bind). The ``path`` is the JSON Pointer into ``next_document``; the
    ``fragment`` is the full added schema fragment (``x-df-binding`` stripped — drift
    does not resolve bindings, and the annotation is not part of the injected value).
    """
    menu: list[dict[str, Any]] = []
    for added in diff_range([effective_document, next_document]).added_fields:
        fragment = _fragment_at(next_document, added.path)
        if fragment is None:
            continue
        menu.append({"path": added.path, "fragment": _strip_binding(fragment)})
    return menu


def _leaf_name(pointer: str) -> str:
    """The property name from a ``…/properties/<name>`` JSON Pointer.

    Nested additions (``/properties/items/items/properties/x``) keep their leaf name
    ``x`` — the engine appends the field by name to the canonical payload; the nested
    structure is the schema document's concern, not the resolver's (the bound
    ``from``/``generated`` source produces the leaf value).
    """
    return pointer.rsplit("/", 1)[-1] if pointer else pointer


def _binding_at(document: dict[str, Any], pointer: str) -> dict[str, Any] | None:
    """The ``x-df-binding`` annotation of the fragment at ``pointer`` (or ``None``)."""
    fragment = _fragment_at(document, pointer)
    if not isinstance(fragment, dict):
        return None
    binding = fragment.get("x-df-binding")
    return binding if isinstance(binding, dict) else None


def _fragment_at(document: Any, pointer: str) -> dict[str, Any] | None:
    """Resolve a JSON Pointer (RFC 6901, unescaped — no ``~`` in our paths) to a dict.

    Our pointers are ``/properties/<name>`` (and nested ``…/items/properties/<name>``)
    over closed JSON Schema documents, so a plain segment walk suffices; a missing
    segment returns ``None`` (a malformed/absent path is skipped by the callers).
    """
    node: Any = document
    for segment in pointer.split("/"):
        if segment == "":
            continue
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node if isinstance(node, dict) else None


def _strip_binding(fragment: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of ``fragment`` without the ``x-df-binding`` annotation."""
    return {k: v for k, v in fragment.items() if k != "x-df-binding"}
