"""Layer-2 resource-bound checks (MAN-V301…V317 ⇔ B-01…B-17, §8.2).

Each bound B-NN maps to error code MAN-V3(NN). Many per-object counts are already
enforced structurally by the §9.1 Layer-1 schema (``maxItems``/``maxProperties``)
and the parse stage (B-01 size, B-02 shape); Layer 2 enforces the **aggregate** and
**cross-cutting** bounds the schema cannot express:

* totals across the document (Σ attributes B-04, subjects B-05, Σ seed B-08, Σ
  background mutations B-14);
* derived counts (entity_refs B-12, payload-size estimate B-12/V503 lives in the
  compat module);
* value-range bounds the schema leaves to Layer 2 (durations ≤ P365D B-15,
  seeding ``min ≤ default ≤ max`` and actor default ≥ 1, intensity coverage).

Codes are assigned per the B-NN → V3NN map below. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ErrorCollector, json_pointer
from .model import ManifestView

# B-NN → MAN-V3NN.  Limits transcribed from the §8.2 bounds table.
MAX_ATTRIBUTES_TOTAL = 2000  # B-04
MAX_SUBJECTS = 250  # B-05
MAX_EVENT_TYPES = 200  # B-05
MAX_SEED_TOTAL = 250_000  # B-08
MAX_ENTITY_REFS = 16  # B-12
MAX_BACKGROUND_MUTATIONS_TOTAL = 20  # B-14
MAX_DURATION_DAYS = 365  # B-15

# A duration component parser (the §9.1 pattern admits only D/H/M/S).
_DUR_RE = re.compile(
    r"^P(?:(?P<days>[0-9]+)D)?(?:T(?:(?P<h>[0-9]+)H)?(?:(?P<m>[0-9]+)M)?"
    r"(?:(?P<s>[0-9]+(?:\.[0-9]+)?)S)?)?$"
)


def check_bounds(view: ManifestView, errors: ErrorCollector) -> None:
    _check_total_attributes(view, errors)  # V304
    _check_subjects_and_event_types(view, errors)  # V305
    _check_seed_totals_and_ranges(view, errors)  # V308
    _check_entity_refs(view, errors)  # V312
    _check_background_mutation_total(view, errors)  # V314
    _check_durations(view, errors)  # V315
    _check_intensity_coverage(view, errors)  # V315 (intensity multipliers)


def _check_total_attributes(view: ManifestView, errors: ErrorCollector) -> None:
    """B-04 / MAN-V304: Σ declared attributes ≤ 2000."""
    total = sum(len(e.attributes) for e in view.entities.values())
    if total > MAX_ATTRIBUTES_TOTAL:
        errors.add(
            "MAN-V304",
            json_pointer("entities"),
            "total declared attributes exceed the document bound",
            bound=MAX_ATTRIBUTES_TOTAL,
            actual=total,
        )


def _check_subjects_and_event_types(view: ManifestView, errors: ErrorCollector) -> None:
    """B-05 / MAN-V305: ≤ 200 business event types and ≤ 250 subjects incl. cdc.*."""
    n_events = len(view.event_types)
    if n_events > MAX_EVENT_TYPES:
        errors.add(
            "MAN-V305",
            json_pointer("event_types"),
            "business event types exceed the bound",
            bound=MAX_EVENT_TYPES,
            actual=n_events,
        )
    n_subjects = n_events + len(view.cdc_entities())
    if n_subjects > MAX_SUBJECTS:
        errors.add(
            "MAN-V305",
            json_pointer("event_types"),
            "derived subjects (business + cdc.*) exceed the bound",
            bound=MAX_SUBJECTS,
            actual=n_subjects,
        )


def _check_seed_totals_and_ranges(view: ManifestView, errors: ErrorCollector) -> None:
    """B-08 / MAN-V308: Σ defaults ≤ 250k; min ≤ default ≤ max; actor default ≥ 1."""
    catalogs = view.seeding.get("catalogs", {})
    total_default = 0
    for ename, cfg in catalogs.items():
        default = int(cfg.get("default", 0))
        total_default += default
        base = json_pointer("seeding", "catalogs", ename)
        lo = cfg.get("min")
        hi = cfg.get("max")
        if lo is not None and default < int(lo):
            errors.add(
                "MAN-V308", base, "seed default is below the catalog minimum",
                bound=int(lo), actual=default,
            )
        if hi is not None and default > int(hi):
            errors.add(
                "MAN-V308", base, "seed default is above the catalog maximum",
                bound=int(hi), actual=default,
            )
        if lo is not None and hi is not None and int(lo) > int(hi):
            errors.add(
                "MAN-V308", base, "seed min exceeds max",
                bound=int(hi), actual=int(lo),
            )
        if ename == view.actor_entity and default < 1:
            errors.add(
                "MAN-V308", base + "/default",
                "actor entity seed default must be >= 1",
                bound=1, actual=default,
            )
    if total_default > MAX_SEED_TOTAL:
        errors.add(
            "MAN-V308",
            json_pointer("seeding", "catalogs"),
            "sum of seed defaults exceeds the document bound",
            bound=MAX_SEED_TOTAL,
            actual=total_default,
        )


def _check_entity_refs(view: ManifestView, errors: ErrorCollector) -> None:
    """B-12 / MAN-V312: derived ``entity_refs`` per event ≤ 16 (R-EVT-5).

    The derived ref set is the partition entity plus each distinct pooled entity
    referenced by payload ``from`` paths and ``ref.fk``-typed fields. We bound the
    upper estimate (distinct created-entities + ref.fk targets in the payload).
    """
    for etype, spec in view.event_types.items():
        payload = spec.get("payload", {})
        refs: set[str] = set()
        for source in payload.values():
            if not isinstance(source, dict):
                continue
            generated = source.get("generated")
            if isinstance(generated, dict) and generated.get("generator") == "ref.fk":
                rel = (generated.get("params", {}) or {}).get("relationship")
                rv = view.relationships_by_name.get(rel) if rel else None
                if rv is not None:
                    refs.add(rv.target_entity)
            raw_from = source.get("from")
            if isinstance(raw_from, str) and raw_from.startswith("created."):
                refs.add(raw_from.split(".")[1])
        if len(refs) > MAX_ENTITY_REFS:
            errors.add(
                "MAN-V312",
                json_pointer("event_types", etype, "payload"),
                "derived entity_refs exceed the envelope bound",
                bound=MAX_ENTITY_REFS,
                actual=len(refs),
            )


def _check_background_mutation_total(view: ManifestView, errors: ErrorCollector) -> None:
    """B-14 / MAN-V314: ≤ 20 background mutations across the whole manifest."""
    total = 0
    for cfg in view.cdc_entities().values():
        total += len(cfg.get("background_mutations", []) or [])
    if total > MAX_BACKGROUND_MUTATIONS_TOTAL:
        errors.add(
            "MAN-V314",
            json_pointer("cdc", "entities"),
            "total background mutations exceed the document bound",
            bound=MAX_BACKGROUND_MUTATIONS_TOTAL,
            actual=total,
        )


def _duration_days(raw: str) -> float | None:
    """Total days a duration represents, or ``None`` if it does not parse."""
    match = _DUR_RE.match(raw)
    if match is None:
        return None
    days = float(match.group("days") or 0)
    hours = float(match.group("h") or 0)
    minutes = float(match.group("m") or 0)
    seconds = float(match.group("s") or 0)
    return days + hours / 24 + minutes / 1440 + seconds / 86400


def _check_durations(view: ManifestView, errors: ErrorCollector) -> None:
    """B-15 / MAN-V315: every dwell/timeout/window duration ≤ P365D."""
    for mname, machine in view.state_machines.items():
        st = machine.get("session_timeout")
        if isinstance(st, str):
            _flag_duration(st, json_pointer("state_machines", mname, "session_timeout"), errors)
        for sname, state in machine.get("states", {}).items():
            timeout = state.get("timeout")
            if isinstance(timeout, dict) and isinstance(timeout.get("after"), str):
                _flag_duration(
                    timeout["after"],
                    json_pointer(
                        "state_machines", mname, "states", sname, "timeout", "after"
                    ),
                    errors,
                )
            for tidx, transition in enumerate(state.get("transitions", []) or []):
                _flag_dwell(
                    transition.get("dwell"),
                    json_pointer(
                        "state_machines", mname, "states", sname,
                        "transitions", tidx, "dwell",
                    ),
                    errors,
                )


def _flag_dwell(dwell: Any, base: str, errors: ErrorCollector) -> None:
    if not isinstance(dwell, dict):
        return
    for key in ("value", "min", "max", "median", "p95", "mean"):
        raw = dwell.get(key)
        if isinstance(raw, str):
            _flag_duration(raw, f"{base}/{key}", errors)


def _flag_duration(raw: str, path: str, errors: ErrorCollector) -> None:
    days = _duration_days(raw)
    if days is not None and days > MAX_DURATION_DAYS:
        errors.add(
            "MAN-V315", path, "duration exceeds the maximum (P365D)",
            bound=MAX_DURATION_DAYS, actual=round(days, 3),
        )


def _check_intensity_coverage(view: ManifestView, errors: ErrorCollector) -> None:
    """§9.1 note (Layer 2): diurnal buckets contiguously cover [0,24), from < to."""
    diurnal = view.intensity.get("diurnal")
    if not isinstance(diurnal, list) or not diurnal:
        return
    buckets = sorted(diurnal, key=lambda b: b.get("from_hour", 0))
    cursor = 0
    base = json_pointer("intensity", "diurnal")
    for idx, bucket in enumerate(buckets):
        frm = int(bucket.get("from_hour", -1))
        to = int(bucket.get("to_hour", -1))
        if frm >= to:
            errors.add(
                "MAN-V315", f"{base}/{idx}",
                "diurnal bucket from_hour must be < to_hour",
                actual=[frm, to],  # type: ignore[arg-type]
            )
            return
        if frm != cursor:
            errors.add(
                "MAN-V315", f"{base}/{idx}",
                "diurnal buckets must contiguously cover [0, 24)",
                bound=cursor, actual=frm,
            )
            return
        cursor = to
    if cursor != 24:
        errors.add(
            "MAN-V315", base,
            "diurnal buckets must cover the full day to hour 24",
            bound=24, actual=cursor,
        )
