"""Context-path resolution for R-DER schema derivation.

Resolves a payload field's ``from`` context path to the concrete JSON Schema
fragment of the value it carries (plugin-arch §5.2 R-DER-2; §5.1 R-EVT-3; the
schema-registry §9.2 ``order_placed`` example is the golden test).

A ``from`` path (the §9.1 ``contextPath`` grammar) is one of:

* ``actor.<attr>[.<sub>...]`` — the actor entity's attribute (e.g.
  ``actor.user_id`` → the key pattern, ``actor.address.country`` → a string);
* ``subject.<attr>`` — the emitting machine's bound entity's attribute;
* ``created.<entity>.<attr>`` — an entity created by the emitting transition;
* ``session.<key>[.<field>]`` — session working memory written by ``remember``
  effects (``session.cart_items`` → an array of the remembered-field object,
  ``session.last_viewed.product_id`` → that remembered field's fragment).

Effect-write resolution (R-DER-2): an entity attribute targeted by any
``create``/``update``/``adjust`` effect (or a ``cdc.background_mutations`` ``set``)
derives its options' base scalar type for ``choice.*`` instead of an enum —
keeping the fragment stable across minor versions (REG-C002 avoidance). This is
only relevant when a ``from`` path lands on a choice attribute.

Pure logic (BE layering: ``infra`` may import ``dataforge_engine``).
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import ManifestView, derive_fragment

_ADDRESS_PROPS = ("street", "city", "state", "postal_code", "country")
_DECIMAL_PATTERN = r"^-?\d+\.\d{1,4}$"


def address_object_fragment() -> dict[str, Any]:
    """The closed five-string ``address.full`` object fragment (R-DER-2)."""
    props = {k: {"type": "string"} for k in _ADDRESS_PROPS}
    return {
        "type": "object",
        "properties": props,
        "required": list(_ADDRESS_PROPS),
        "additionalProperties": False,
    }


def generated_fragment(
    view: ManifestView, generated: dict[str, Any], key_prefixes: dict[str, str]
) -> dict[str, Any]:
    """A bare ``generated`` spec → fragment, with the registry's R-DER-2 extensions.

    Two cases the engine's static ``derive_fragment`` does not cover (it has no
    manifest context) are handled here first, then everything else delegates to the
    engine so the rest of the type mapping is single-sourced:

    * ``derived.expr`` derives its declared ``params.output`` type (``decimal`` →
      the S-6 money decimal-string pattern, ``integer`` → integer, ``number`` →
      number) — the engine maps only the static catalog output (``number``);
    * ``ref.fk`` derives the *target entity's* key pattern (relationship →
      target → key_prefix), which needs the relationship index — the engine keys
      its lookup by relationship name and so returns a plain string.
    """
    name = generated.get("generator", "")
    if name == "derived.expr":
        output = (generated.get("params", {}) or {}).get("output", "number")
        if output == "decimal":
            return {"type": "string", "pattern": _DECIMAL_PATTERN}
        if output == "integer":
            return {"type": "integer"}
        return {"type": "number"}
    if name == "ref.fk":
        return ref_fk_fragment(view, generated, key_prefixes)
    return derive_fragment({"generated": generated}, key_prefixes)


def ref_fk_fragment(
    view: ManifestView, generator_spec: dict[str, Any], key_prefixes: dict[str, str]
) -> dict[str, Any]:
    """``ref.fk`` → the entity-key pattern of the relationship's *target* entity."""
    rel_name = (generator_spec.get("params", {}) or {}).get("relationship", "")
    rel = view.relationships_by_name.get(rel_name)
    if rel is None:
        return {"type": "string"}
    prefix = key_prefixes.get(rel.target_entity, "")
    if prefix:
        return {"type": "string", "pattern": f"^{prefix}_[0-9a-f]{{16}}$"}
    return {"type": "string"}


def effect_written_attributes(view: ManifestView) -> set[tuple[str, str]]:
    """Set of ``(entity, attribute)`` written by any effect or background mutation.

    Drives the R-DER-2 effect-write rule (a ``choice.*`` attribute in this set
    derives its base scalar type, not an enum).
    """
    written: set[tuple[str, str]] = set()
    for machine in view.state_machines.values():
        for state in (machine.get("states", {}) or {}).values():
            for transition in state.get("transitions", []) or []:
                for effect in transition.get("effects", []) or []:
                    _collect_effect_targets(view, effect, written)
    for entity_name, cdc in view.cdc_entities().items():
        for mutation in cdc.get("background_mutations", []) or []:
            for attr in (mutation.get("set", {}) or {}):
                written.add((entity_name, attr))
    return written


def _collect_effect_targets(
    view: ManifestView, effect: dict[str, Any], written: set[tuple[str, str]]
) -> None:
    action = effect.get("action")
    if action == "create":
        created_entity = str(effect.get("entity", ""))
        for attr in (effect.get("set", {}) or {}):
            written.add((created_entity, attr))
    elif action in ("update", "adjust"):
        target_entity = _resolve_target_entity(view, str(effect.get("target", "")))
        if target_entity is None:
            return
        if action == "adjust":
            attr = effect.get("attribute")
            if attr:
                written.add((target_entity, str(attr)))
        else:
            for attr in (effect.get("set", {}) or {}):
                written.add((target_entity, attr))


def _resolve_target_entity(view: ManifestView, target: str) -> str | None:
    """Map an effect ``target`` (actor|subject|created.x[.via.rel]) to its entity."""
    if not target:
        return None
    parts = target.split(".via.")
    head = parts[0]
    if head == "actor":
        entity: str | None = view.actor_entity or None
    elif head == "subject":
        entity = _lifecycle_bound_entity(view)
    elif head.startswith("created."):
        entity = head.split(".", 1)[1]
    else:
        entity = None
    for rel_name in parts[1:]:
        rel = view.relationships_by_name.get(rel_name)
        entity = rel.target_entity if rel is not None else None
    return entity


def _lifecycle_bound_entity(view: ManifestView) -> str | None:
    for machine in view.state_machines.values():
        if machine.get("type") == "lifecycle":
            return str(machine.get("binds", "")) or None
    return view.actor_entity or None


def resolve_from_path(
    view: ManifestView,
    path: str,
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
    subject_entity: str | None,
) -> dict[str, Any]:
    """Resolve a ``from`` context path to its concrete JSON Schema fragment."""
    head, _, rest = path.partition(".")
    if head == "actor":
        return _resolve_entity_path(view, view.actor_entity, rest, key_prefixes, effect_written)
    if head == "subject":
        return _resolve_entity_path(view, subject_entity, rest, key_prefixes, effect_written)
    if head == "created":
        entity, _, attr_path = rest.partition(".")
        return _resolve_entity_path(view, entity, attr_path, key_prefixes, effect_written)
    if head == "session":
        return _resolve_session_path(view, rest, key_prefixes, effect_written)
    return {}


def _resolve_entity_path(
    view: ManifestView,
    entity_name: str | None,
    attr_path: str,
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """Resolve ``<attr>[.<sub>]`` against an entity's declared attributes/key."""
    entity = view.entities.get(entity_name or "")
    if entity is None:
        return {}
    first, _, sub = attr_path.partition(".")
    if first == entity.key_attribute:
        return {"type": "string", "pattern": f"^{entity.key_prefix}_[0-9a-f]{{16}}$"}
    generator_spec = entity.attributes.get(first)
    if generator_spec is None:
        return {}  # auto timestamps / unknown — MAN-V105 already proved resolvable
    if generator_spec.get("generator") == "address.full" and sub:
        return {"type": "string"}  # actor.address.country → the sub-property string
    return entity_attribute_fragment(
        view, entity_name or "", first, generator_spec, key_prefixes, effect_written
    )


def entity_attribute_fragment(
    view: ManifestView,
    entity_name: str,
    attr_name: str,
    generator_spec: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """Concrete fragment for an entity attribute by its declared generator (R-DER-2).

    Shared by CDC row-image derivation, ``from``-path resolution, and ``ref.attr``
    chaining so the type mapping is single-sourced.
    """
    name = generator_spec.get("generator", "")
    if name == "address.full":
        return address_object_fragment()
    if name == "ref.fk":
        return ref_fk_fragment(view, generator_spec, key_prefixes)
    if name == "ref.attr":
        return _ref_attr_fragment(view, entity_name, generator_spec, key_prefixes, effect_written)
    if name in ("choice.uniform", "choice.weighted"):
        return _choice_path_fragment(generator_spec, effect_written, entity_name, attr_name)
    return generated_fragment(view, generator_spec, key_prefixes)


def _ref_attr_fragment(
    view: ManifestView,
    entity_name: str,
    generator_spec: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """``ref.attr`` copies a related attribute's type (resolve via the sibling fk)."""
    params = generator_spec.get("params", {}) or {}
    via_attr = params.get("via", "")
    target_attr = params.get("attribute", "")
    entity = view.entities.get(entity_name)
    if entity is None:
        return {}
    fk_spec = entity.attributes.get(via_attr, {})
    rel_name = (fk_spec.get("params", {}) or {}).get("relationship", "")
    rel = view.relationships_by_name.get(rel_name)
    if rel is None:
        return {}
    return _resolve_entity_path(
        view, rel.target_entity, target_attr, key_prefixes, effect_written
    )


def _choice_path_fragment(
    generator_spec: dict[str, Any],
    effect_written: set[tuple[str, str]],
    entity_name: str,
    attr_name: str,
) -> dict[str, Any]:
    values = _choice_values(generator_spec)
    if (entity_name, attr_name) in effect_written:
        return {"type": _scalar_type_of(values)}
    return {"enum": values}


def _resolve_session_path(
    view: ManifestView,
    rest: str,
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """Resolve ``<key>[.<field>]`` against the ``remember`` effects that write it.

    A ``set`` remember writes an object; ``append`` writes a list of that object.
    ``session.cart_items`` → array of the remembered-field object; ``session.
    last_viewed.product_id`` → that single field's fragment.
    """
    key, _, field = rest.partition(".")
    mode, value_fields = _remember_spec_for(view, key)
    if value_fields is None:
        return {}
    if field:
        source = value_fields.get(field)
        if source is None:
            return {}
        return _resolve_value_field(view, source, key_prefixes, effect_written, value_fields)
    obj = _remember_object_fragment(view, value_fields, key_prefixes, effect_written)
    if mode == "append":
        return {"type": "array", "items": obj}
    return obj


def _remember_object_fragment(
    view: ManifestView,
    value_fields: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    props = {
        name: _resolve_value_field(view, src, key_prefixes, effect_written, value_fields)
        for name, src in value_fields.items()
    }
    return {
        "type": "object",
        "properties": props,
        # Sorted for byte-identical derivation regardless of manifest key order
        # (R-DER-5; see _closed_document in registry.infra.derive).
        "required": sorted(props.keys()),
        "additionalProperties": False,
    }


def _resolve_value_field(
    view: ManifestView,
    source: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
    siblings: dict[str, Any],
) -> dict[str, Any]:
    """A remember-field ``valueSource`` → fragment (const / generated / from).

    ``siblings`` are the other fields of the same remember value, so a ``ref.attr``
    whose ``via`` names a sibling ``ref.fk`` field can resolve through it (the
    cart's ``unit_price`` = the viewed product's price, schema-registry §9.2).
    """
    if "const" in source:
        return {"const": source["const"]}
    if "generated" in source:
        generated = source["generated"]
        if generated.get("generator") == "ref.attr":
            return _ref_attr_in_memory(view, generated, siblings, key_prefixes, effect_written)
        return generated_fragment(view, generated, key_prefixes)
    if "from" in source:
        return resolve_from_path(view, str(source["from"]), key_prefixes, effect_written, None)
    return {}


def _ref_attr_in_memory(
    view: ManifestView,
    generated: dict[str, Any],
    siblings: dict[str, Any],
    key_prefixes: dict[str, str],
    effect_written: set[tuple[str, str]],
) -> dict[str, Any]:
    """``ref.attr`` inside a remember value: ``via`` is a sibling ``ref.fk`` field."""
    params = generated.get("params", {}) or {}
    via_field = params.get("via", "")
    target_attr = params.get("attribute", "")
    via_source = siblings.get(via_field, {})
    via_gen = via_source.get("generated", {}) if isinstance(via_source, dict) else {}
    rel_name = (via_gen.get("params", {}) or {}).get("relationship", "")
    rel = view.relationships_by_name.get(rel_name)
    if rel is None:
        return {}
    return _resolve_entity_path(
        view, rel.target_entity, target_attr, key_prefixes, effect_written
    )


def _remember_spec_for(
    view: ManifestView, key: str
) -> tuple[str, dict[str, Any] | None]:
    """Find the first ``remember`` effect writing ``key``; return (mode, value)."""
    for machine in view.state_machines.values():
        for state in (machine.get("states", {}) or {}).values():
            for transition in state.get("transitions", []) or []:
                for effect in transition.get("effects", []) or []:
                    if effect.get("action") == "remember" and effect.get("key") == key:
                        return str(effect.get("mode", "set")), dict(effect.get("value", {}) or {})
    return "set", None


def _choice_values(generator_spec: dict[str, Any]) -> list[Any]:
    raw = (generator_spec.get("params", {}) or {}).get("options", [])
    values: list[Any] = []
    for opt in raw:
        if isinstance(opt, dict) and "value" in opt:
            values.append(opt["value"])
        else:
            values.append(opt)
    return values


def _scalar_type_of(values: list[Any]) -> str:
    if all(isinstance(v, bool) for v in values):
        return "boolean"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "integer"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "number"
    return "string"
