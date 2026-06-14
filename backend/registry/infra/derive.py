"""R-DER schema derivation: a published manifest → closed JSON Schema documents.

This is the registry-side half of derivation (schema-registry §5.1 / plugin-arch
§5.2 R-DER-1..3). For a manifest it produces, per derived subject, a **closed**
JSON Schema document (``additionalProperties: false``, every property
``required`` — R-DER-3) whose property fragments are resolved to concrete types:

* business event subject ``{slug}.{event_type}`` — one per declared event type;
* CDC subject ``{slug}.cdc.{entity}`` — one per ``cdc.entities`` entry, the row
  image (all declared attributes + key + auto ``created_at``/``updated_at``,
  R-DER-1).

The R-DER-2 *generator* type-mapping is single-sourced in
``dataforge_engine.manifest.derive_fragment``; this module adds the **context-path
resolution** the registry needs: a payload field ``{from: actor.address.country}``
resolves to the concrete fragment of the referenced entity attribute (a string),
``{from: created.orders.order_id}`` to the entity-key pattern, ``{from:
session.cart_items}`` to an array of the remembered-field object (the §9.2
normative example is the golden test).

Derivation is deterministic: the same canonical manifest yields byte-identical
comparison forms (golden-tested). Pure logic — no Django imports beyond the engine
package (BE layering: ``infra`` may import ``dataforge_engine``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dataforge_engine.manifest import ManifestView
from registry.infra.resolve import (
    effect_written_attributes,
    entity_attribute_fragment,
    generated_fragment,
    resolve_from_path,
)

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_ID_BASE = "https://docs.dataforge.dev/schemas"

# Auto-maintained entity timestamps (plugin-arch §3.1) present on every row image.
_AUTO_TIMESTAMPS = ("created_at", "updated_at")


@dataclass(frozen=True)
class DerivedSubject:
    """One derived subject + its closed v1 schema document (annotations included)."""

    subject: str
    event_type: str  # business event type, or "cdc.{entity}" for CDC subjects
    is_cdc: bool
    document: dict[str, Any]  # full closed-profile JSON Schema (R-DER-3)


def derive_subjects(manifest: dict[str, Any]) -> list[DerivedSubject]:
    """Derive every subject (business + CDC) for ``manifest`` (R-DER-1..3).

    Returns subjects in a deterministic order: business event types in manifest
    declaration order, then CDC subjects in ``cdc.entities`` declaration order.
    """
    view = ManifestView(manifest)
    slug = view.slug
    key_prefixes = {ent.name: ent.key_prefix for ent in view.entities.values()}
    effect_written = effect_written_attributes(view)
    emit_contexts = _event_emit_contexts(view)
    out: list[DerivedSubject] = []

    for event_type, spec in view.event_types.items():
        properties = _event_payload_schema(
            view, event_type, spec, key_prefixes, effect_written, emit_contexts
        )
        subject = f"{slug}.{event_type}"
        out.append(
            DerivedSubject(
                subject=subject,
                event_type=event_type,
                is_cdc=False,
                document=_closed_document(subject, 1, properties, f"event type {event_type}"),
            )
        )

    for entity_name in view.cdc_entities():
        properties = _cdc_row_image_schema(view, entity_name, key_prefixes, effect_written)
        subject = f"{slug}.cdc.{entity_name}"
        out.append(
            DerivedSubject(
                subject=subject,
                event_type=f"cdc.{entity_name}",
                is_cdc=True,
                document=_closed_document(
                    subject, 1, properties, f"CDC row image for entity {entity_name}"
                ),
            )
        )
    return out


def _closed_document(
    subject: str, version: int, properties: dict[str, Any], origin: str
) -> dict[str, Any]:
    """Assemble a closed-profile JSON Schema (R-DER-3, SD-1/SD-5).

    ``required`` is sorted so derivation is byte-identical regardless of the
    manifest's property-declaration order (R-DER-5). The publish path derives from
    ``ManifestVersion.manifest`` read back through JSONB, which does **not** preserve
    object key order, while a fresh derivation reads the canonical declaration-order
    document; a sorted ``required`` array makes both produce the same bytes. The
    closed-profile invariant ``set(required) == set(properties)`` (every field
    required) is unchanged — only the array order is canonicalized.
    """
    return {
        "$schema": _SCHEMA_DIALECT,
        "$id": f"{_ID_BASE}/{subject}/versions/{version}.json",
        "title": f"{subject} v{version}",
        "description": f"Derived from manifest {subject.split('.')[0]}, {origin}.",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties.keys()),
        "properties": properties,
    }


def _event_emit_contexts(view: ManifestView) -> dict[str, str | None]:
    """Map each event type → the entity its ``subject`` resolves to when emitted.

    The emitting machine binds ``subject``: a lifecycle machine binds its entity,
    a session machine binds the actor entity. An event emitted by transitions of
    one machine has a single ``subject`` entity (MAN-V105 already proved the
    payload paths resolve in *every* emitting context, so any emitting transition
    gives the same answer for the registry's purposes).
    """
    contexts: dict[str, str | None] = {}
    for machine in view.state_machines.values():
        bound = (
            str(machine.get("binds", "")) or None
            if machine.get("type") == "lifecycle"
            else view.actor_entity or None
        )
        for state in (machine.get("states", {}) or {}).values():
            for transition in state.get("transitions", []) or []:
                emit = transition.get("emit")
                if emit:
                    contexts.setdefault(str(emit), bound)
            timeout = state.get("timeout") or {}
            if timeout.get("emit"):
                contexts.setdefault(str(timeout["emit"]), bound)
    return contexts


def _event_payload_schema(
    view: ManifestView,
    event_type: str,
    spec: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
    emit_contexts: dict[str, str | None],
) -> dict[str, Any]:
    """Resolve every payload field of an event type to its concrete fragment."""
    payload: dict[str, Any] = spec.get("payload", {}) or {}
    subject_entity = emit_contexts.get(event_type)
    props: dict[str, Any] = {}
    for field_name, source in payload.items():
        props[field_name] = _resolve_value_source(
            view, source, key_prefixes, effect_written, subject_entity
        )
    return props


def _resolve_value_source(
    view: ManifestView,
    source: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
    subject_entity: str | None,
) -> dict[str, Any]:
    """A payload ``valueSource`` -> concrete fragment (const / generated / from)."""
    nullable = bool(source.get("nullable", False))
    if "const" in source:
        return {"const": source["const"]}
    if "generated" in source:
        # Payload ``generated`` choice fields always derive enums (R-DER-2);
        # ``generated_fragment`` adds the ``derived.expr(output: ...)`` handling
        # the engine's static catalog mapping omits.
        return _wrap_nullable(generated_fragment(view, source["generated"], key_prefixes), nullable)
    if "from" in source:
        fragment = resolve_from_path(
            view, str(source["from"]), key_prefixes, effect_written, subject_entity
        )
        return _wrap_nullable(fragment, nullable)
    return {}


def _wrap_nullable(fragment: dict[str, Any], nullable: bool) -> dict[str, Any]:
    if not nullable or "type" not in fragment:
        return fragment
    base = fragment["type"]
    if isinstance(base, str):
        fragment = dict(fragment)
        fragment["type"] = [base, "null"]
    return fragment


def _cdc_row_image_schema(
    view: ManifestView,
    entity_name: str,
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """The CDC row image: key + every declared attribute + auto timestamps (R-DER-1)."""
    entity = view.entities.get(entity_name)
    props: dict[str, Any] = {}
    if entity is None:
        return props
    props[entity.key_attribute] = {
        "type": "string",
        "pattern": f"^{entity.key_prefix}_[0-9a-f]{{16}}$",
    }
    for attr_name, generator_spec in entity.attributes.items():
        props[attr_name] = entity_attribute_fragment(
            view, entity_name, attr_name, generator_spec, key_prefixes, effect_written
        )
    for ts in _AUTO_TIMESTAMPS:
        props[ts] = {"type": "string", "format": "date-time"}
    return props
