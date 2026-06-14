"""Compiled Manifest IR — the immutable, executable form of a manifest the
interpreter runs (behavior-engine §1.1, §2.3, §11).

The IR compiler turns a Layer-1/2-valid manifest document into immutable runtime
structures: per-state cumulative probability tables, compiled guards, generator
closures bound to sub-seeds, dwell specs, timeout edges, and self-transitions —
everything §6.2 selection needs without re-parsing the document per timer. The
compiled IR is cached (LRU keyed ``slug:version``) because a published manifest
version is immutable (P-6): compile once, run many streams.

This module defines the IR **data classes**; the compiler lives in
:func:`compile_manifest`. Generic by construction: zero scenario knowledge — the
IR is a faithful, executable projection of whatever manifest is compiled.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dataforge_engine.manifest import GENERATOR_CATALOG, ManifestView

from .distributions import DwellSpec, compile_dwell, parse_duration_us
from .errors import CompileError
from .generators import GeneratorFn, build_generator

if TYPE_CHECKING:
    from dataforge_engine.envelope.types import JSONValue

RemainderPolicy = Literal["exit", "stay"]


# ---------------------------------------------------------------------------
# Value sources (payload fields, effect ``set`` values).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueSource:
    """A compiled ``valueSource`` (plugin-architecture §9.1): exactly one of
    ``from`` (context path), ``const`` (literal), or ``generated`` (a bound
    generator closure). ``nullable`` permits a resolved ``None``.
    """

    kind: Literal["from", "const", "generated"]
    path: str | None = None
    const: JSONValue = None
    generator: GeneratorFn | None = None
    nullable: bool = False


def compile_value_source(spec: dict[str, Any]) -> ValueSource:
    nullable = bool(spec.get("nullable", False))
    if "from" in spec:
        return ValueSource("from", path=str(spec["from"]), nullable=nullable)
    if "const" in spec:
        return ValueSource("const", const=spec["const"], nullable=nullable)
    if "generated" in spec:
        gspec = spec["generated"]
        gen = _compile_generator(gspec)
        return ValueSource("generated", generator=gen, nullable=nullable)
    raise CompileError(f"value source has none of from/const/generated: {spec!r}")


def _compile_generator(gspec: dict[str, Any]) -> GeneratorFn:
    name = str(gspec["generator"])
    if name not in GENERATOR_CATALOG:
        raise CompileError(f"unknown generator {name!r} (not in catalog)")
    params: dict[str, Any] = dict(gspec.get("params", {}))
    return build_generator(name, params)


# ---------------------------------------------------------------------------
# Guards (compiled preconditions; behavior-engine §5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    """A compiled attribute comparison (``{path, op, value}``)."""

    path: str
    op: str
    value: JSONValue


@dataclass(frozen=True)
class ExistsCondition:
    """A compiled ``exists`` over a declared relationship."""

    relationship: str
    of: str
    negate: bool
    where: tuple[tuple[str, str, JSONValue | None, str | None], ...]
    # each where: (attribute, op, literal_value, ref_path)


@dataclass(frozen=True)
class Guard:
    """A conjunction of compiled conditions (``all``); empty = always-true."""

    comparisons: tuple[Comparison, ...]
    exists: tuple[ExistsCondition, ...]

    @property
    def is_empty(self) -> bool:
        return not self.comparisons and not self.exists


_EMPTY_GUARD = Guard((), ())


def compile_guard(spec: dict[str, Any] | None) -> Guard:
    if not spec:
        return _EMPTY_GUARD
    conditions = spec.get("all", [])
    comparisons: list[Comparison] = []
    exists: list[ExistsCondition] = []
    for cond in conditions:
        if "exists" in cond:
            ex = cond["exists"]
            where: list[tuple[str, str, JSONValue | None, str | None]] = []
            for w in ex.get("where", []):
                where.append(
                    (str(w["attribute"]), str(w["op"]), w.get("value"), w.get("ref"))
                )
            exists.append(
                ExistsCondition(
                    relationship=str(ex["relationship"]),
                    of=str(ex["of"]),
                    negate=bool(ex.get("negate", False)),
                    where=tuple(where),
                )
            )
        else:
            comparisons.append(Comparison(str(cond["path"]), str(cond["op"]), cond.get("value")))
    return Guard(tuple(comparisons), tuple(exists))


# ---------------------------------------------------------------------------
# Effects (compiled mutations; plugin-architecture §6.4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """A compiled effect. ``action`` ∈ create/update/adjust/delete/remember.

    ``set_sources`` are compiled value sources keyed by attribute/field name (in
    declaration order — that order IS the CDC emission order, R-CDC-2).
    """

    action: Literal["create", "update", "adjust", "delete", "remember"]
    entity: str | None = None  # create
    target: str | None = None  # update/adjust/delete
    attribute: str | None = None  # adjust
    by_const: float | None = None  # adjust literal
    by_path: str | None = None  # adjust path
    key: str | None = None  # remember
    mode: str | None = None  # remember set|append
    set_sources: tuple[tuple[str, ValueSource], ...] = ()


def compile_effect(spec: dict[str, Any]) -> Effect:
    action = str(spec["action"])
    if action == "create":
        sets = _compile_sets(spec.get("set", {}))
        return Effect("create", entity=str(spec["entity"]), set_sources=sets)
    if action == "update":
        return Effect("update", target=str(spec["target"]), set_sources=_compile_sets(spec["set"]))
    if action == "adjust":
        by = spec["by"]
        if isinstance(by, int | float):
            return Effect("adjust", target=str(spec["target"]),
                          attribute=str(spec["attribute"]), by_const=float(by))
        return Effect("adjust", target=str(spec["target"]),
                      attribute=str(spec["attribute"]), by_path=str(by))
    if action == "delete":
        return Effect("delete", target=str(spec["target"]))
    if action == "remember":
        return Effect("remember", key=str(spec["key"]), mode=str(spec["mode"]),
                      set_sources=_compile_sets(spec["value"]))
    raise CompileError(f"unknown effect action {action!r}")


def _compile_sets(raw: dict[str, Any]) -> tuple[tuple[str, ValueSource], ...]:
    return tuple((name, compile_value_source(spec)) for name, spec in raw.items())


# ---------------------------------------------------------------------------
# Transitions, states, machines.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """A compiled transition with its cumulative-probability upper bound.

    ``cumulative`` is Σ p_j for j ≤ this transition (declaration order), so §6.2
    selection is a single walk: the first transition whose ``cumulative`` exceeds
    the draw ``u`` is selected; ``u ≥ S`` (the last cumulative) selects the
    remainder policy.
    """

    to: str
    probability: float
    cumulative: float
    dwell: DwellSpec
    guard: Guard
    effects: tuple[Effect, ...]
    emit: str | None


@dataclass(frozen=True)
class TimeoutEdge:
    """A compiled state-level ``timeout {after, to, emit?}`` (§6.2 rule 5)."""

    after_us: int
    to: str
    emit: str | None
    effects: tuple[Effect, ...]


@dataclass(frozen=True)
class State:
    """A compiled state: the cumulative transition table, remainder policy,
    optional timeout edge, terminal flag, and the next selection's dwell context.
    """

    name: str
    terminal: bool
    remainder: RemainderPolicy
    sum_probability: float
    transitions: tuple[Transition, ...]
    timeout: TimeoutEdge | None


@dataclass(frozen=True)
class Machine:
    """A compiled state machine (session or lifecycle)."""

    name: str
    kind: Literal["session", "lifecycle"]
    binds: str
    initial: str
    session_timeout_us: int | None  # session machines only
    states: dict[str, State]


@dataclass(frozen=True)
class EventTypeIR:
    """A compiled event type: partition entity-ref + ordered payload sources."""

    name: str
    partition_by: str
    payload: tuple[tuple[str, ValueSource], ...]


@dataclass(frozen=True)
class EntityIR:
    """A compiled entity type: key prefix/attribute + ordered attribute generators."""

    name: str
    key_prefix: str
    key_attribute: str
    attributes: tuple[tuple[str, GeneratorFn], ...]
    cdc_enabled: bool
    cdc_ops: frozenset[str]


@dataclass
class ManifestIR:
    """The fully compiled, immutable manifest the interpreter executes."""

    slug: str
    version: str
    actor_entity: str
    simulated_timezone: str
    entities: dict[str, EntityIR]
    entity_order: tuple[str, ...]
    event_types: dict[str, EventTypeIR]
    machines: dict[str, Machine]
    session_machine: str
    lifecycle_by_entity: dict[str, str]  # bound entity type → lifecycle machine name
    relationships: tuple[tuple[str, str, str, str], ...]
    # each rel: (name, source_entity, source_attribute, target_entity)
    seeding: dict[str, int] = field(default_factory=dict)
    schema_versions: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The compiler.
# ---------------------------------------------------------------------------


def _compile_transitions(raw: list[dict[str, Any]]) -> tuple[tuple[Transition, ...], float]:
    transitions: list[Transition] = []
    cumulative = 0.0
    for t in raw:
        prob = float(t["probability"])
        cumulative += prob
        transitions.append(
            Transition(
                to=str(t["to"]),
                probability=prob,
                cumulative=cumulative,
                dwell=compile_dwell(t.get("dwell")),
                guard=compile_guard(t.get("guard")),
                effects=tuple(compile_effect(e) for e in t.get("effects", [])),
                emit=t.get("emit"),
            )
        )
    return tuple(transitions), cumulative


def _compile_state(name: str, raw: dict[str, Any]) -> State:
    if raw.get("terminal"):
        return State(name, True, "exit", 0.0, (), None)
    transitions, total = _compile_transitions(raw.get("transitions", []))
    remainder: RemainderPolicy = raw.get("remainder", "exit")
    timeout = None
    if "timeout" in raw:
        to_raw = raw["timeout"]
        timeout = TimeoutEdge(
            after_us=parse_duration_us(str(to_raw["after"])),
            to=str(to_raw["to"]),
            emit=to_raw.get("emit"),
            effects=tuple(compile_effect(e) for e in to_raw.get("effects", [])),
        )
    return State(name, False, remainder, total, transitions, timeout)


def _compile_machine(name: str, raw: dict[str, Any]) -> Machine:
    kind = str(raw["type"])
    session_timeout_us = None
    if kind == "session":
        session_timeout_us = parse_duration_us(str(raw.get("session_timeout", "PT30M")))
    states = {sname: _compile_state(sname, sraw) for sname, sraw in raw["states"].items()}
    return Machine(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        binds=str(raw["binds"]),
        initial=str(raw["initial"]),
        session_timeout_us=session_timeout_us,
        states=states,
    )


def _compile_entities(view: ManifestView) -> dict[str, EntityIR]:
    cdc_entities = view.cdc_entities()
    entities: dict[str, EntityIR] = {}
    for name, ent in view.entities.items():
        attrs: list[tuple[str, GeneratorFn]] = []
        for attr_name, gspec in ent.attributes.items():
            attrs.append((attr_name, _compile_generator(gspec)))
        cdc_cfg = cdc_entities.get(name, {})
        entities[name] = EntityIR(
            name=name,
            key_prefix=ent.key_prefix,
            key_attribute=ent.key_attribute,
            attributes=tuple(attrs),
            cdc_enabled=bool(cdc_cfg.get("enabled_default", False)),
            cdc_ops=frozenset(cdc_cfg.get("ops", [])),
        )
    return entities


def _compile_event_types(view: ManifestView) -> dict[str, EventTypeIR]:
    event_types: dict[str, EventTypeIR] = {}
    for name, et in view.event_types.items():
        payload = tuple(
            (fname, compile_value_source(fspec)) for fname, fspec in et["payload"].items()
        )
        event_types[name] = EventTypeIR(
            name=name,
            partition_by=str(et.get("partition_by", "actor")),
            payload=payload,
        )
    return event_types


def compile_manifest(
    document: dict[str, Any], *, schema_versions: dict[str, int] | None = None
) -> ManifestIR:
    """Compile a Layer-1/2-valid manifest document into an executable IR."""
    view = ManifestView(document)
    machines = {n: _compile_machine(n, m) for n, m in view.state_machines.items()}

    session_machine = ""
    lifecycle_by_entity: dict[str, str] = {}
    for mname, machine in machines.items():
        if machine.kind == "session":
            session_machine = mname
        else:
            lifecycle_by_entity[machine.binds] = mname
    if not session_machine:
        raise CompileError("manifest declares no session machine (MAN-V210)")

    seeding: dict[str, int] = {}
    for ename, cfg in view.seeding.get("catalogs", {}).items():
        seeding[ename] = int(cfg.get("default", 0))

    relationships = tuple(
        (r.name, r.source_entity, r.source_attribute, r.target_entity)
        for r in view.relationships
    )

    return ManifestIR(
        slug=view.slug,
        version=str(view.metadata.get("version", "")),
        actor_entity=view.actor_entity,
        simulated_timezone=str(view.metadata.get("simulated_timezone", "UTC")),
        entities=_compile_entities(view),
        entity_order=tuple(view.entity_order),
        event_types=_compile_event_types(view),
        machines=machines,
        session_machine=session_machine,
        lifecycle_by_entity=lifecycle_by_entity,
        relationships=relationships,
        seeding=seeding,
        schema_versions=dict(schema_versions or {}),
    )


# ---------------------------------------------------------------------------
# LRU cache keyed slug:version (published versions are immutable, P-6).
# ---------------------------------------------------------------------------

_CACHE: OrderedDict[str, ManifestIR] = OrderedDict()
_CACHE_MAX = 32


def compile_manifest_cached(
    document: dict[str, Any],
    *,
    config_sha256: str = "",
    schema_versions: dict[str, int] | None = None,
) -> ManifestIR:
    """LRU-cached :func:`compile_manifest`, keyed ``slug:version:config_sha256``.

    Per-stream merged config can change probabilities/dwells, so the key includes
    ``config_sha256``; with the default empty sha (golden/dry-run paths that pass
    the pinned document) the key reduces to ``slug:version``.
    """
    view = ManifestView(document)
    key = f"{view.slug}:{view.metadata.get('version', '')}:{config_sha256}"
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    ir = compile_manifest(document, schema_versions=schema_versions)
    _CACHE[key] = ir
    _CACHE.move_to_end(key)
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return ir


def clear_ir_cache() -> None:
    """Drop the IR cache (tests / config reloads)."""
    _CACHE.clear()
