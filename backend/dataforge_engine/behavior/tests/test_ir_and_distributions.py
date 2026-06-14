"""IR compiler + LRU cache + distribution-sampling fixed-draw accounting.

Covers: the IR compiles the synthetic manifest into the expected machine/event/
entity structures, the LRU cache returns the same instance for ``slug:version``,
the closed dwell families parse and sample within bounds, and ``fixed`` consumes
no draw (the §7.3 accounting that makes cursors restorable).
"""

from __future__ import annotations

from dataforge_engine.behavior import compile_manifest, compile_manifest_cached
from dataforge_engine.behavior.distributions import (
    DWELL_CEILING_US,
    DwellSpec,
    compile_dwell,
    parse_duration_us,
)
from dataforge_engine.behavior.ir import clear_ir_cache

from .fixtures import synthetic_manifest


def test_compile_produces_session_and_lifecycle() -> None:
    ir = compile_manifest(synthetic_manifest())
    assert ir.session_machine == "shopping"
    assert ir.lifecycle_by_entity == {"orders": "order_lifecycle"}
    assert ir.actor_entity == "users"
    assert set(ir.entities) == {"users", "products", "orders"}
    assert ir.entities["users"].cdc_enabled is True
    assert ir.entities["orders"].cdc_ops == frozenset({"c", "u"})


def test_lru_cache_returns_same_instance() -> None:
    clear_ir_cache()
    doc = synthetic_manifest()
    a = compile_manifest_cached(doc)
    b = compile_manifest_cached(doc)
    assert a is b
    clear_ir_cache()
    c = compile_manifest_cached(doc)
    assert c is not a


def test_duration_parsing() -> None:
    assert parse_duration_us("PT1S") == 1_000_000
    assert parse_duration_us("PT3M") == 180_000_000
    assert parse_duration_us("P1D") == 86_400 * 1_000_000
    assert parse_duration_us("PT1.5S") == 1_500_000


def test_fixed_dwell_consumes_no_draw() -> None:
    spec = compile_dwell({"family": "fixed", "value": "PT5S"})
    assert spec.needs_draw is False
    assert spec.sample_fixed_value() == 5_000_000


def test_lognormal_dwell_clamped_at_ceiling() -> None:
    spec = compile_dwell({"family": "lognormal", "median": "P1D", "p95": "P10D"})
    assert spec.needs_draw is True
    # Sampling at u → 1 must clamp at the B-15 ceiling, never overflow.
    assert spec.sample(0.999999999999) <= DWELL_CEILING_US


def test_uniform_dwell_within_bounds() -> None:
    spec = compile_dwell({"family": "uniform", "min": "PT1S", "max": "PT10S"})
    for u in (0.0, 0.5, 0.9999):
        sample = spec.sample(u)
        assert 1_000_000 <= sample <= 10_000_000


def test_default_dwell_is_zero_fixed() -> None:
    spec = compile_dwell(None)
    assert isinstance(spec, DwellSpec)
    assert spec.needs_draw is False
    assert spec.sample_fixed_value() == 0
