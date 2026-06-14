"""Layer-2 referential-integrity checks (MAN-V101…V111, §8.2).

Every cross-reference in the manifest must name a declared entity, attribute, or
relationship, resolve in its binding context, and respect the reserved-name and
seed-order rules. These checks make guards/effects/payloads structurally sound
**before** the runtime ever evaluates them (INV-GEN-1/2).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Any

from .errors import ErrorCollector, json_pointer
from .model import AUTO_ATTRIBUTES, ManifestView
from .paths import parse_context_path, parse_entity_ref

_DF_PREFIX = "_df"


# Guard ops that require a numeric attribute / a timestamp attribute (MAN-V104).
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte"})
_TEMPORAL_OPS = frozenset({"within"})


def check_referential(view: ManifestView, errors: ErrorCollector) -> None:
    _check_entity_names(view, errors)
    _check_reserved_and_shadow(view, errors)
    _check_relationships(view, errors)
    _check_cdc_entities(view, errors)
    _check_session_binding_entities(view, errors)
    _check_event_payload_paths(view, errors)
    _check_emit_closure(view, errors)
    _check_ref_attr_and_templates(view, errors)
    _check_guards_and_effects(view, errors)
    _check_seed_order_dag(view, errors)


def _entity_exists(view: ManifestView, name: str) -> bool:
    return name in view.entities


def _check_entity_names(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V101: every referenced entity name is declared."""
    actor = view.actor_entity
    if actor and not _entity_exists(view, actor):
        errors.add(
            "MAN-V101",
            json_pointer("metadata", "actor_entity"),
            "actor_entity references an undeclared entity",
            actual=actor,
        )
    for rel in view.relationships:
        base = json_pointer("relationships", rel.index)
        if not _entity_exists(view, rel.source_entity):
            errors.add(
                "MAN-V101",
                base + "/source_entity",
                "relationship source_entity is not declared",
                actual=rel.source_entity,
            )
        if not _entity_exists(view, rel.target_entity):
            errors.add(
                "MAN-V101",
                base + "/target_entity",
                "relationship target_entity is not declared",
                actual=rel.target_entity,
            )
    for entity in view.seeded_entities():
        if not _entity_exists(view, entity):
            errors.add(
                "MAN-V101",
                json_pointer("seeding", "catalogs", entity),
                "seeding catalog names an undeclared entity",
                actual=entity,
            )


def _check_session_binding_entities(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V101: machine ``binds`` names a declared entity."""
    for mname, machine in view.state_machines.items():
        binds = machine.get("binds", "")
        if binds and not _entity_exists(view, binds):
            errors.add(
                "MAN-V101",
                json_pointer("state_machines", mname, "binds"),
                "state machine binds an undeclared entity",
                actual=binds,
            )


def _check_reserved_and_shadow(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V109 (_df reserved) and MAN-V110 (created_at/updated_at, key shadow)."""
    for ename, entity in view.entities.items():
        for attr in entity.attributes:
            if attr.startswith(_DF_PREFIX):
                errors.add(
                    "MAN-V109",
                    json_pointer("entities", ename, "attributes", attr),
                    "attribute uses the reserved _df prefix",
                    actual=attr,
                )
            if attr in AUTO_ATTRIBUTES:
                errors.add(
                    "MAN-V110",
                    json_pointer("entities", ename, "attributes", attr),
                    "attribute name collides with an auto-maintained timestamp",
                    actual=attr,
                )
            if attr == entity.key_attribute:
                errors.add(
                    "MAN-V110",
                    json_pointer("entities", ename, "attributes", attr),
                    "attribute shadows the entity key_attribute",
                    actual=attr,
                )


def _check_relationships(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V102: relationship source_attribute is declared on the source entity."""
    for rel in view.relationships:
        src = view.entities.get(rel.source_entity)
        if src is None:
            continue  # V101 already reported the missing entity
        if rel.source_attribute not in src.declared_attribute_names():
            errors.add(
                "MAN-V102",
                json_pointer("relationships", rel.index, "source_attribute"),
                "relationship source_attribute is not declared on the source entity",
                actual=rel.source_attribute,
            )


def _check_cdc_entities(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V108: cdc.entities keys are declared; V102 for background_mutations set."""
    for ename, cfg in view.cdc_entities().items():
        if not _entity_exists(view, ename):
            errors.add(
                "MAN-V108",
                json_pointer("cdc", "entities", ename),
                "cdc.entities names an undeclared entity",
                actual=ename,
            )
            continue
        entity = view.entities[ename]
        declared = entity.declared_attribute_names()
        for midx, mutation in enumerate(cfg.get("background_mutations", []) or []):
            for attr in (mutation.get("set", {}) or {}).keys():
                if attr not in declared:
                    errors.add(
                        "MAN-V102",
                        json_pointer(
                            "cdc",
                            "entities",
                            ename,
                            "background_mutations",
                            midx,
                            "set",
                            attr,
                        ),
                        "background mutation writes an undeclared attribute",
                        actual=attr,
                    )


def _emit_created_entities(view: ManifestView) -> dict[str, set[str]]:
    """Map each event type → entities created by *some* emitting transition.

    A payload ``from: created.X.*`` only resolves if at least one transition that
    emits the event creates entity ``X`` via a ``create`` effect (R-EVT-3). This
    index drives the MAN-V105/V106 cross-context closure check.
    """
    out: dict[str, set[str]] = {etype: set() for etype in view.event_types}
    for machine in view.state_machines.values():
        for state in machine.get("states", {}).values():
            for transition in state.get("transitions", []) or []:
                emit = transition.get("emit")
                if not isinstance(emit, str) or emit not in out:
                    continue
                for effect in transition.get("effects", []) or []:
                    if effect.get("action") == "create" and isinstance(
                        effect.get("entity"), str
                    ):
                        out[emit].add(effect["entity"])
    return out


def _check_event_payload_paths(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V105/V106: payload ``from`` paths and ``partition_by`` resolve.

    A ``created.<entity>`` reference must (a) name a declared entity and (b) be
    created by some transition that emits this event type (cross-context closure,
    R-EVT-3). An unresolvable created-context reference is the canonical V105 case
    the spec calls out.
    """
    emit_created = _emit_created_entities(view)
    for etype, spec in view.event_types.items():
        payload = spec.get("payload", {})
        created_here = emit_created.get(etype, set())
        for field_name, source in payload.items():
            raw_from = source.get("from") if isinstance(source, dict) else None
            if not raw_from:
                continue
            path = parse_context_path(raw_from)
            if path.kind != "created":
                continue
            entity = path.created_entity or ""
            fpath = json_pointer("event_types", etype, "payload", field_name, "from")
            if not _entity_exists(view, entity):
                errors.add(
                    "MAN-V105", fpath,
                    "payload 'from' references an undeclared created entity",
                    actual=entity,
                )
            elif entity not in created_here:
                errors.add(
                    "MAN-V105", fpath,
                    "payload 'from' references a created entity not created by any "
                    "transition emitting this event type",
                    actual=entity,
                )
        partition = spec.get("partition_by")
        if isinstance(partition, str):
            ref = parse_entity_ref(partition)
            if ref.kind == "created":
                entity = ref.created_entity or ""
                ppath = json_pointer("event_types", etype, "partition_by")
                if not _entity_exists(view, entity):
                    errors.add(
                        "MAN-V106", ppath,
                        "partition_by references an undeclared created entity",
                        actual=entity,
                    )
                elif entity not in created_here:
                    errors.add(
                        "MAN-V106", ppath,
                        "partition_by references a created entity not created by any "
                        "emitting transition",
                        actual=entity,
                    )


def _check_emit_closure(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V107: every ``emit`` names a declared event type, and vice versa (R-EVT-6)."""
    declared = set(view.event_types.keys())
    emitted: set[str] = set()
    for mname, machine in view.state_machines.items():
        for sname, state in machine.get("states", {}).items():
            for tidx, transition in enumerate(state.get("transitions", []) or []):
                emit = transition.get("emit")
                if isinstance(emit, str):
                    emitted.add(emit)
                    if emit not in declared:
                        errors.add(
                            "MAN-V107",
                            json_pointer(
                                "state_machines",
                                mname,
                                "states",
                                sname,
                                "transitions",
                                tidx,
                                "emit",
                            ),
                            "emit names an undeclared event type",
                            actual=emit,
                        )
            timeout = state.get("timeout")
            if isinstance(timeout, dict) and isinstance(timeout.get("emit"), str):
                emit = timeout["emit"]
                emitted.add(emit)
                if emit not in declared:
                    errors.add(
                        "MAN-V107",
                        json_pointer(
                            "state_machines", mname, "states", sname, "timeout", "emit"
                        ),
                        "timeout emit names an undeclared event type",
                        actual=emit,
                    )
    for etype in sorted(declared - emitted):
        errors.add(
            "MAN-V107",
            json_pointer("event_types", etype),
            "event type is never emitted by any transition (R-EVT-6)",
            actual=etype,
        )


def _entity_uses_ref_fk_targets(
    entity_attrs: dict[str, dict[str, Any]], view: ManifestView
) -> list[str]:
    """Target entities reached by ``ref.fk`` generators on an entity's attributes."""
    targets: list[str] = []
    for spec in entity_attrs.values():
        if not isinstance(spec, dict) or spec.get("generator") != "ref.fk":
            continue
        rel_name = (spec.get("params", {}) or {}).get("relationship")
        rel = view.relationships_by_name.get(rel_name) if rel_name else None
        if rel is not None:
            targets.append(rel.target_entity)
    return targets


def _check_seed_order_dag(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V111: seeded entities' ``ref.fk`` targets are seeded and declared earlier.

    The seed-time reference graph over seeded entities must be a DAG respecting
    declaration order — every seeded entity may only ``ref.fk`` an entity that is
    also seeded and appears **earlier** in ``entities`` (behaviour-engine §4.5).
    """
    seeded = view.seeded_entities()
    order_index = {name: i for i, name in enumerate(view.entity_order)}
    for ename in seeded:
        entity = view.entities.get(ename)
        if entity is None:
            continue
        self_idx = order_index.get(ename, -1)
        for attr, spec in entity.attributes.items():
            if not isinstance(spec, dict) or spec.get("generator") != "ref.fk":
                continue
            rel_name = (spec.get("params", {}) or {}).get("relationship")
            rel = view.relationships_by_name.get(rel_name) if rel_name else None
            if rel is None:
                continue  # V103 reports the bad relationship reference
            target = rel.target_entity
            target_idx = order_index.get(target, -1)
            path = json_pointer("entities", ename, "attributes", attr)
            if target not in seeded:
                errors.add(
                    "MAN-V111",
                    path,
                    "seeded entity ref.fk targets an unseeded entity",
                    actual=target,
                )
            elif target_idx >= self_idx:
                errors.add(
                    "MAN-V111",
                    path,
                    "seeded entity ref.fk targets an entity not declared earlier",
                    actual=target,
                )


def _check_ref_attr_and_templates(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V103 (ref.fk/ref.attr relationships), MAN-V102 (ref.attr target attr)."""
    for ename, entity in view.entities.items():
        for attr, spec in entity.attributes.items():
            if not isinstance(spec, dict):
                continue
            generator = spec.get("generator")
            params = spec.get("params", {}) or {}
            base = json_pointer("entities", ename, "attributes", attr)
            if generator == "ref.fk":
                rel_name = params.get("relationship")
                rel = view.relationships_by_name.get(rel_name) if rel_name else None
                if rel is None:
                    errors.add(
                        "MAN-V103",
                        base + "/params/relationship",
                        "ref.fk names an undeclared relationship",
                        actual=rel_name,
                    )
                elif rel.source_entity != ename:
                    errors.add(
                        "MAN-V103",
                        base + "/params/relationship",
                        "ref.fk relationship source does not match this entity",
                        actual=rel_name,
                    )
            elif generator == "ref.attr":
                via = params.get("via")
                via_spec = entity.attributes.get(via) if via else None
                if not isinstance(via_spec, dict) or via_spec.get("generator") != "ref.fk":
                    errors.add(
                        "MAN-V102",
                        base + "/params/via",
                        "ref.attr 'via' is not a sibling ref.fk attribute",
                        actual=via,
                    )
                    continue
                rel_name = (via_spec.get("params", {}) or {}).get("relationship")
                rel = view.relationships_by_name.get(rel_name) if rel_name else None
                target_entity = view.entities.get(rel.target_entity) if rel else None
                target_attr = params.get("attribute")
                if (
                    target_entity is not None
                    and target_attr not in target_entity.declared_attribute_names()
                ):
                    errors.add(
                        "MAN-V102",
                        base + "/params/attribute",
                        "ref.attr target attribute is not declared on the target entity",
                        actual=target_attr,
                    )


def _check_guards_and_effects(view: ManifestView, errors: ErrorCollector) -> None:
    """MAN-V103 (guard exists rels) + MAN-V102/V104 (guard attrs/ops).

    A guard's ``subject.<attr>`` comparison resolves against the machine's bound
    entity (``binds``); an exists-guard's ``where`` attributes resolve against the
    relationship's source entity (the entity the FK is held on).
    """
    for mname, machine in view.state_machines.items():
        bound = machine.get("binds", "")
        for sname, state in machine.get("states", {}).items():
            for tidx, transition in enumerate(state.get("transitions", []) or []):
                guard = transition.get("guard")
                if not isinstance(guard, dict):
                    continue
                tbase = json_pointer(
                    "state_machines", mname, "states", sname, "transitions", tidx, "guard"
                )
                for cidx, cond in enumerate(guard.get("all", []) or []):
                    _check_guard_condition(
                        view, cond, bound, f"{tbase}/all/{cidx}", errors
                    )


def _check_guard_condition(
    view: ManifestView,
    cond: dict[str, Any],
    bound_entity: str,
    base: str,
    errors: ErrorCollector,
) -> None:
    if "exists" in cond:
        exists = cond["exists"]
        rel_name = exists.get("relationship")
        rel = view.relationships_by_name.get(rel_name) if rel_name else None
        if rel is None:
            errors.add(
                "MAN-V103",
                base + "/exists/relationship",
                "exists-guard names an undeclared relationship",
                actual=rel_name,
            )
            return
        target = view.entities.get(rel.source_entity)
        for widx, where in enumerate(exists.get("where", []) or []):
            attr = where.get("attribute")
            if target is not None and attr not in target.declared_attribute_names():
                errors.add(
                    "MAN-V102",
                    base + f"/exists/where/{widx}/attribute",
                    "exists-guard where attribute is not declared",
                    actual=attr,
                )
            _check_op_type(
                view, rel.source_entity, attr, where.get("op"),
                base + f"/exists/where/{widx}", errors,
            )
        return
    # comparison: { path, op, value } — subject.<attr> resolves against bound entity.
    raw_path = cond.get("path", "")
    op = cond.get("op")
    ctx = parse_context_path(raw_path) if raw_path else None
    if ctx is None or ctx.kind != "subject" or not bound_entity:
        return
    attr = ctx.first_segment
    entity = view.entities.get(bound_entity)
    if attr is None or entity is None:
        return
    if attr not in entity.declared_attribute_names():
        errors.add(
            "MAN-V102",
            base + "/path",
            "guard path references an attribute not declared on the bound entity",
            actual=attr,
        )
        return
    _check_op_type(view, bound_entity, attr, op, base + "/path", errors)


def _check_op_type(
    view: ManifestView,
    entity_name: str,
    attribute: str | None,
    op: str | None,
    base: str,
    errors: ErrorCollector,
) -> None:
    """MAN-V104: numeric/temporal ops require a compatible attribute fragment."""
    if attribute is None or op is None:
        return
    entity = view.entities.get(entity_name)
    if entity is None:
        return
    spec = entity.attributes.get(attribute)
    if not isinstance(spec, dict):
        return  # key/auto attribute; op compatibility not statically known
    from .generators import GENERATOR_CATALOG  # local import avoids a cycle at module load

    gspec = GENERATOR_CATALOG.get(spec.get("generator", ""))
    if gspec is None:
        return
    if op in _NUMERIC_OPS and gspec.output not in {"integer", "number", "decimal"}:
        errors.add(
            "MAN-V104",
            base,
            "numeric comparison op used on a non-numeric attribute",
            actual=op,
        )
    if op in _TEMPORAL_OPS and gspec.output != "datetime":
        errors.add(
            "MAN-V104",
            base,
            "'within' op requires a timestamp attribute",
            actual=op,
        )
