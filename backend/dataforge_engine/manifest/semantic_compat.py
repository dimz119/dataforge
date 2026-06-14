"""Layer-2 schema-compatibility + effect-type checks (MAN-V407, V501…V503, §8.2).

* V407 — effect-write type mismatch: every value a ``create``/``update`` effect
  ``set``, an ``adjust``, or a ``cdc.background_mutations`` ``set`` writes to an
  entity attribute must satisfy that attribute's derived R-DER-2 fragment, so CDC
  row images and ``from``-mapped payload fields can never fail at emission.
* V501 — a derived payload schema is non-additive vs the prior manifest version
  (R-DER-4 / BACKWARD_ADDITIVE). Checked against an injected
  :class:`PriorSchemaProvider`; ``None`` (first publication / Phase-3 default)
  skips it.
* V502 — derived subject-set collision (R-DER-5): an entity named like an event
  type, with CDC enabled, would collide ``slug.cdc.x`` with a business subject.
* V503 — worst-case serialized payload estimate > 64 KiB (B-12).

The fragment derivation is single-sourced in :mod:`derive` (the registry/Phase-4
schema-derivation reuses it). Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any, Protocol

from .derive import derive_fragment, fragment_size_estimate
from .errors import ErrorCollector, json_pointer
from .generators import GENERATOR_CATALOG, NUMERIC_OUTPUTS
from .model import ManifestView

MAX_PAYLOAD_BYTES = 64 * 1024  # B-12


class PriorSchemaProvider(Protocol):
    """Supplies the previously-registered derived schema for a subject (R-DER-4).

    The catalog app injects an implementation backed by the schema registry; the
    validator uses it to enforce BACKWARD_ADDITIVE at the manifest (MAN-V501),
    failing publication before the registry is touched. ``None`` means no prior
    version (first publication) — V501 is then vacuous.
    """

    def latest_payload_schema(self, subject: str) -> dict[str, Any] | None: ...


def check_compat(
    view: ManifestView,
    errors: ErrorCollector,
    *,
    prior_schemas: PriorSchemaProvider | None = None,
) -> None:
    _check_effect_write_types(view, errors)  # V407
    _check_subject_collisions(view, errors)  # V502
    _check_payload_size(view, errors)  # V503
    if prior_schemas is not None:
        _check_backward_additive(view, errors, prior_schemas)  # V501


def _key_prefix_by_relationship(view: ManifestView) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in view.relationships:
        target = view.entities.get(rel.target_entity)
        if target is not None:
            out[rel.name] = target.key_prefix
    return out


def _attribute_output_kind(view: ManifestView, entity: str, attribute: str) -> str | None:
    """The R-DER-2 output kind of an entity attribute, or ``None`` if unknown."""
    spec = view.entity_attribute_generator(entity, attribute)
    if spec is None:
        return None
    gspec = GENERATOR_CATALOG.get(spec.get("generator", ""))
    return gspec.output if gspec is not None else None


def _check_effect_write_types(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V407: effect/adjust/background writes match the target attribute fragment."""
    # cdc.background_mutations set (target entity is the cdc entity itself).
    for ename, cfg in view.cdc_entities().items():
        for midx, mutation in enumerate(cfg.get("background_mutations", []) or []):
            for attr, spec in (mutation.get("set", {}) or {}).items():
                base = json_pointer(
                    "cdc", "entities", ename, "background_mutations", midx, "set", attr
                )
                _check_generated_write(view, ename, attr, spec, base, errors)

    # effect set/adjust targets are resolved through entity-ref + .via hops; the
    # full target-entity resolution needs the machine binding, so we type-check the
    # statically-resolvable cases: create.set (target entity == effect.entity) and
    # adjust (numeric attribute requirement).
    for mname, machine in view.state_machines.items():
        bound = machine.get("binds", "")
        for sname, state in machine.get("states", {}).items():
            for tidx, transition in enumerate(state.get("transitions", []) or []):
                for eidx, effect in enumerate(transition.get("effects", []) or []):
                    _check_effect(
                        view, bound, effect,
                        json_pointer(
                            "state_machines", mname, "states", sname,
                            "transitions", tidx, "effects", eidx,
                        ),
                        errors,
                    )


def _check_effect(
    view: ManifestView,
    bound_entity: str,
    effect: dict[str, Any],
    base: str,
    errors: ErrorCollector,
) -> None:
    action = effect.get("action")
    if action == "create":
        target = effect.get("entity", "")
        for attr, vs in (effect.get("set", {}) or {}).items():
            _check_value_source_write(
                view, target, attr, vs, f"{base}/set/{attr}", errors
            )
    elif action == "adjust":
        # adjust requires a numeric target attribute fragment.
        target_attr = effect.get("attribute", "")
        target_entity = _resolve_self_target_entity(view, bound_entity, effect.get("target", ""))
        kind = (
            _attribute_output_kind(view, target_entity, target_attr)
            if target_entity
            else None
        )
        if kind is not None and kind not in NUMERIC_OUTPUTS:
            errors.add(
                "MAN-V407", base + "/attribute",
                "adjust requires a numeric target attribute", actual=kind,
            )


def _resolve_self_target_entity(
    view: ManifestView, bound_entity: str, target: str
) -> str | None:
    """Resolve a no-hop entity-ref target (``actor``/``subject``/``created.x``).

    ``.via`` hops require relationship traversal that depends on machine context;
    those are deferred to the Phase-4 dry run. ``actor`` resolves to the actor
    entity, ``subject`` to the machine's bound entity, ``created.x`` to ``x``.
    """
    if target == "subject":
        return bound_entity or None
    if target == "actor":
        return view.actor_entity or None
    if target.startswith("created."):
        return target.split(".via.")[0][len("created.") :]
    return None  # .via hops deferred


def _check_value_source_write(
    view: ManifestView,
    entity: str,
    attribute: str,
    value_source: dict[str, Any],
    base: str,
    errors: ErrorCollector,
) -> None:
    """MAN-V407: a ``const`` / ``generated`` write matches the attribute fragment."""
    target_kind = _attribute_output_kind(view, entity, attribute)
    if target_kind is None:
        return  # unknown attribute already reported by V102 / not type-checkable
    if "const" in value_source:
        _check_const_kind(value_source["const"], target_kind, base, errors)
    elif "generated" in value_source:
        _check_generated_write(view, entity, attribute, value_source["generated"], base, errors)


def _check_generated_write(
    view: ManifestView,
    entity: str,
    attribute: str,
    generated: dict[str, Any],
    base: str,
    errors: ErrorCollector,
) -> None:
    target_kind = _attribute_output_kind(view, entity, attribute)
    gen_name = generated.get("generator", "") if isinstance(generated, dict) else ""
    src = GENERATOR_CATALOG.get(gen_name)
    if target_kind is None or src is None:
        return
    if not _kinds_compatible(src.output, target_kind):
        errors.add(
            "MAN-V407", base,
            "effect-written value type does not match the target attribute",
            bound=target_kind, actual=src.output,
        )


def _check_const_kind(value: Any, target_kind: str, base: str, errors: ErrorCollector) -> None:
    kind = _const_kind(value)
    if not _kinds_compatible(kind, target_kind):
        errors.add(
            "MAN-V407", base,
            "const value type does not match the target attribute",
            bound=target_kind, actual=kind,
        )


def _const_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _kinds_compatible(written: str, target: str) -> bool:
    """Whether a written output kind satisfies a target attribute's R-DER-2 fragment."""
    if written == target:
        return True
    numeric = {"integer", "number", "decimal"}
    if written in numeric and target in numeric:
        return True
    # choice.* / ref.attr resolve downstream; do not flag.
    if written in {"enum", "scalar", "hook"} or target in {"enum", "scalar", "hook"}:
        return True
    # money decimals are strings on the wire; a string write to a decimal is fine.
    if {written, target} <= {"string", "decimal"}:
        return True
    return False


def _check_subject_collisions(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V502: derived subjects (business + cdc.*) must be collision-free (R-DER-5)."""
    seen: dict[str, str] = {}
    for etype in view.event_types:
        seen[etype] = "event_type"
    for ename in view.cdc_entities():
        subject = f"cdc.{ename}"
        if subject in seen:
            errors.add(
                "MAN-V502", json_pointer("cdc", "entities", ename),
                "derived cdc subject collides with a business event subject",
                actual=subject,
            )
        seen[subject] = "cdc"


def _check_payload_size(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V503: worst-case serialized payload estimate ≤ 64 KiB (B-12)."""
    prefixes = _key_prefix_by_relationship(view)
    for etype, spec in view.event_types.items():
        total = 0
        for source in spec.get("payload", {}).values():
            if isinstance(source, dict):
                total += fragment_size_estimate(derive_fragment(source, prefixes))
        if total > MAX_PAYLOAD_BYTES:
            errors.add(
                "MAN-V503", json_pointer("event_types", etype),
                "worst-case serialized payload estimate exceeds 64 KiB",
                bound=MAX_PAYLOAD_BYTES, actual=total,
            )


def _check_backward_additive(
    view: ManifestView,
    errors: ErrorCollector,
    prior: PriorSchemaProvider,
) -> None:
    """MAN-V501: a changed derived payload schema must be BACKWARD_ADDITIVE (R-DER-4).

    For each event type, compare the derived payload property set against the
    prior registered schema: removing or retyping a property is non-additive.
    Adding optional/new properties is permitted (the registry assigns a new
    version). The registry owns the full algorithm (schema-registry §6); this is
    the manifest-side fail-fast (fail at the manifest, not the registry).
    """
    prefixes = _key_prefix_by_relationship(view)
    for etype, spec in view.event_types.items():
        subject = f"{view.slug}.{etype}"
        previous = prior.latest_payload_schema(subject)
        if previous is None:
            continue
        prior_props = set((previous.get("properties") or {}).keys())
        new_props = set(spec.get("payload", {}).keys())
        removed = sorted(prior_props - new_props)
        if removed:
            errors.add(
                "MAN-V501", json_pointer("event_types", etype, "payload"),
                "derived payload removes a previously-registered field (non-additive)",
                actual=removed[0],
            )
        # Retype check: a shared field whose derived fragment 'type' changed.
        prior_field_schemas = previous.get("properties") or {}
        for field_name in sorted(prior_props & new_props):
            source = spec["payload"][field_name]
            if not isinstance(source, dict):
                continue
            new_type = derive_fragment(source, prefixes).get("type")
            old_type = (prior_field_schemas.get(field_name) or {}).get("type")
            if old_type is not None and new_type is not None and new_type != old_type:
                errors.add(
                    "MAN-V501",
                    json_pointer("event_types", etype, "payload", field_name),
                    "derived payload field changed type (non-additive)",
                    bound=old_type, actual=new_type,
                )
