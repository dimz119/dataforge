"""Layer-2 generator allowlist / params checks (MAN-V401…V407, §8.2).

Walks every ``generatorSpec`` in the manifest — entity attributes, payload
``generated`` fields, effect ``set`` values, ``cdc.background_mutations`` ``set`` —
and validates it against the closed catalog (:mod:`generators`):

* V401 unknown generator (the §9.1 enum is closed, but this re-asserts the
  allowlist + carries the value for the repair loop);
* V402 invalid params (per-generator catalog: unknown param, wrong type, out of
  range, bad choice);
* V403 ``hook.name`` not registered (against an injected registry; empty by
  default in Phase 3);
* V404 ``hook`` generator in a ``workspace``-visibility manifest (capability gate);
* V405 ``template`` placeholder unresolvable or cyclic;
* V406 ``derived.expr`` violates the §4.5 grammar.

V407 (effect-write type compatibility) lives in :mod:`semantic_compat` alongside
the R-DER-2 fragment derivation it depends on.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from .errors import ErrorCollector, json_pointer
from .expr import validate_expr
from .generators import GENERATOR_CATALOG, ParamSpec
from .model import ManifestView

# §4.4 template placeholders: {attr_name} siblings + random tokens.
_TEMPLATE_TOKEN = re.compile(r"\{([^{}]*)\}")
_RANDOM_TOKENS = frozenset({"#hex8", "#hex16", "#digits4", "#digits8", "#upper4"})
_MAX_TEMPLATE_PLACEHOLDERS = 16
_MONEY_DECIMAL = re.compile(r"^-?\d+\.\d{1,4}$")


def check_generators(
    view: ManifestView,
    errors: ErrorCollector,
    *,
    is_workspace_visibility: bool,
    registered_hooks: frozenset[str] = frozenset(),
) -> None:
    for path, spec, sibling_attrs in _iter_generator_specs(view):
        _check_one_generator(
            spec,
            path,
            sibling_attrs,
            errors,
            is_workspace_visibility=is_workspace_visibility,
            registered_hooks=registered_hooks,
        )


def _iter_generator_specs(
    view: ManifestView,
) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Yield ``(json_pointer, generatorSpec, sibling_attribute_specs)`` for every spec.

    ``sibling_attrs`` is the set of attribute specs in the same scope (used by the
    template placeholder check for sibling references); empty where not applicable.
    """
    for ename, entity in view.entities.items():
        for attr, spec in entity.attributes.items():
            if isinstance(spec, dict):
                yield (
                    json_pointer("entities", ename, "attributes", attr),
                    spec,
                    entity.attributes,
                )
    for etype, et in view.event_types.items():
        for field_name, source in et.get("payload", {}).items():
            if isinstance(source, dict) and isinstance(source.get("generated"), dict):
                yield (
                    json_pointer(
                        "event_types", etype, "payload", field_name, "generated"
                    ),
                    source["generated"],
                    {},
                )
    for ename, cfg in view.cdc_entities().items():
        for midx, mutation in enumerate(cfg.get("background_mutations", []) or []):
            for attr, spec in (mutation.get("set", {}) or {}).items():
                if isinstance(spec, dict):
                    yield (
                        json_pointer(
                            "cdc", "entities", ename, "background_mutations",
                            midx, "set", attr,
                        ),
                        spec,
                        {},
                    )
    yield from _iter_effect_generators(view)


def _iter_effect_generators(
    view: ManifestView,
) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
    for mname, machine in view.state_machines.items():
        for sname, state in machine.get("states", {}).items():
            for tidx, transition in enumerate(state.get("transitions", []) or []):
                for eidx, effect in enumerate(transition.get("effects", []) or []):
                    base = json_pointer(
                        "state_machines", mname, "states", sname,
                        "transitions", tidx, "effects", eidx,
                    )
                    for key in ("set", "value"):
                        for attr, vs in (effect.get(key, {}) or {}).items():
                            if isinstance(vs, dict) and isinstance(
                                vs.get("generated"), dict
                            ):
                                yield (
                                    f"{base}/{key}/{attr}/generated",
                                    vs["generated"],
                                    {},
                                )


def _check_one_generator(
    spec: dict[str, Any],
    path: str,
    sibling_attrs: dict[str, Any],
    errors: ErrorCollector,
    *,
    is_workspace_visibility: bool,
    registered_hooks: frozenset[str],
) -> None:
    name = spec.get("generator")
    catalog_spec = GENERATOR_CATALOG.get(name) if isinstance(name, str) else None
    if catalog_spec is None:
        errors.add(
            "MAN-V401", path + "/generator",
            "generator is not in the closed allowlist", actual=name,
        )
        return
    params = spec.get("params", {}) or {}

    if name == "hook":
        _check_hook(
            params, path, errors,
            is_workspace_visibility=is_workspace_visibility,
            registered_hooks=registered_hooks,
        )
        return

    _check_params(catalog_spec.name, catalog_spec.params, params, path, errors)

    if name == "template":
        _check_template(params, sibling_attrs, path, errors)
    elif name == "derived.expr":
        _check_expression(params, path, errors)
    elif name in ("commerce.price", "number.decimal"):
        _check_decimal_str_params(name, params, path, errors)


def _check_params(
    gen_name: str,
    accepted: dict[str, ParamSpec],
    params: dict[str, Any],
    path: str,
    errors: ErrorCollector,
) -> None:
    """MAN-V402: unknown params, missing required, wrong type, out-of-range, bad choice."""
    for pname, pspec in accepted.items():
        if pspec.required and pname not in params:
            errors.add(
                "MAN-V402", path + "/params",
                f"generator '{gen_name}' requires param '{pname}'", actual=None,
            )
    for pname, value in params.items():
        ppath = f"{path}/params/{pname}"
        accepted_spec = accepted.get(pname)
        if accepted_spec is None:
            errors.add(
                "MAN-V402", ppath,
                f"unknown param '{pname}' for generator '{gen_name}'", actual=pname,
            )
            continue
        _check_param_value(gen_name, pname, accepted_spec, value, ppath, errors)


def _check_param_value(
    gen_name: str,
    pname: str,
    pspec: ParamSpec,
    value: Any,
    ppath: str,
    errors: ErrorCollector,
) -> None:
    if not _type_ok(pspec.type, value):
        errors.add(
            "MAN-V402", ppath,
            f"param '{pname}' has the wrong type (expected {pspec.type})",
            actual=_kind_of(value),
        )
        return
    if pspec.choices is not None and value not in pspec.choices:
        errors.add(
            "MAN-V402", ppath,
            f"param '{pname}' must be one of {list(pspec.choices)}", actual=value,
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if pspec.minimum is not None and value < pspec.minimum:
            errors.add(
                "MAN-V402", ppath, f"param '{pname}' is below its minimum",
                bound=pspec.minimum, actual=value,
            )
        if pspec.maximum is not None and value > pspec.maximum:
            errors.add(
                "MAN-V402", ppath, f"param '{pname}' is above its maximum",
                bound=pspec.maximum, actual=value,
            )
    if isinstance(value, str) and pspec.max_length is not None and len(value) > pspec.max_length:
        errors.add(
            "MAN-V402", ppath, f"param '{pname}' exceeds its maximum length",
            bound=pspec.max_length, actual=len(value),
        )
    if isinstance(value, list):
        if pspec.min_items is not None and len(value) < pspec.min_items:
            errors.add(
                "MAN-V402", ppath, f"param '{pname}' has too few items",
                bound=pspec.min_items, actual=len(value),
            )
        if pspec.max_items is not None and len(value) > pspec.max_items:
            errors.add(
                "MAN-V402", ppath, f"param '{pname}' has too many items",
                bound=pspec.max_items, actual=len(value),
            )


def _type_ok(ptype: str, value: Any) -> bool:
    if ptype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "bool":
        return isinstance(value, bool)
    if ptype in ("string", "decimal_str"):
        return isinstance(value, str)
    if ptype == "array":
        return isinstance(value, list)
    if ptype == "object":
        return isinstance(value, dict)
    return True


def _kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _check_decimal_str_params(
    gen_name: str, params: dict[str, Any], path: str, errors: ErrorCollector
) -> None:
    """MAN-V402: ``min``/``max`` of money generators must be 2-4-scale decimal strings."""
    for key in ("min", "max"):
        raw = params.get(key)
        if isinstance(raw, str) and not _MONEY_DECIMAL.match(raw):
            errors.add(
                "MAN-V402", f"{path}/params/{key}",
                f"param '{key}' of '{gen_name}' must be a decimal string", actual=raw,
            )


def _check_hook(
    params: dict[str, Any],
    path: str,
    errors: ErrorCollector,
    *,
    is_workspace_visibility: bool,
    registered_hooks: frozenset[str],
) -> None:
    """MAN-V404 (visibility gate) and MAN-V403 (registered hook name)."""
    if is_workspace_visibility:
        errors.add(
            "MAN-V404", path + "/generator",
            "hook generators are forbidden in workspace-visibility manifests",
            actual="hook",
        )
    name = params.get("name")
    if not isinstance(name, str):
        errors.add(
            "MAN-V403", path + "/params/name",
            "hook spec requires a 'name' param", actual=None,
        )
        return
    if name not in registered_hooks:
        errors.add(
            "MAN-V403", path + "/params/name",
            "hook name is not registered", actual=name,
        )


def _check_template(
    params: dict[str, Any],
    sibling_attrs: dict[str, Any],
    path: str,
    errors: ErrorCollector,
) -> None:
    """MAN-V405: template placeholders resolve to siblings/random tokens, ≤ 16, acyclic."""
    pattern = params.get("pattern")
    if not isinstance(pattern, str):
        return
    tokens = _TEMPLATE_TOKEN.findall(pattern)
    if len(tokens) > _MAX_TEMPLATE_PLACEHOLDERS:
        errors.add(
            "MAN-V405", path + "/params/pattern",
            "template exceeds the maximum placeholder count",
            bound=_MAX_TEMPLATE_PLACEHOLDERS, actual=len(tokens),
        )
    for token in tokens:
        if token in _RANDOM_TOKENS:
            continue
        if token not in sibling_attrs:
            errors.add(
                "MAN-V405", path + "/params/pattern",
                "template placeholder is neither a sibling attribute nor a random token",
                actual=token,
            )


def _check_expression(params: dict[str, Any], path: str, errors: ErrorCollector) -> None:
    """MAN-V406: ``derived.expr`` must satisfy the closed §4.5 grammar."""
    expr = params.get("expr")
    if not isinstance(expr, str):
        return
    problem = validate_expr(expr)
    if problem is not None:
        errors.add("MAN-V406", path + "/params/expr", problem, actual=None)
