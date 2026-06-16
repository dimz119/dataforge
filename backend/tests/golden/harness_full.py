"""The full-manifest (ecommerce 1.1.0 + CDC + curves) pure-engine batch harness.

GOLD-B, CDC-1..7, STAT-SHAPE/F/L, and the 1M full-manifest PROP-RI profile all
drive the *same* generic engine over the builtin **full** manifest
(``catalog/builtin/ecommerce/1.1.0.yaml`` — 8 entities, ~21 business event types,
4 default-on CDC subjects, diurnal/weekly intensity curves) under a deterministic
injected wall clock, then assert over the produced canonical envelopes.

This is the Phase-8 sibling of :mod:`tests.golden.harness` (which pins the 1.0.0
subset for GOLD-A / the legacy PROP profiles). It is intentionally a *separate*
module so the subset harness — and the committed GOLD-A fixture — never move when
the full manifest evolves. Everything here is pure: it touches only
``dataforge_engine`` (framework-free) and ``generation.infra.clock`` (a tiny pure
host seam), so the full suites ride the fast engine lane (no Postgres, no Redis).

The full manifest emits CDC ``c``/``u`` rows derived from the SAME pool mutations
as the business events (ADR-0012, PoolTransaction's two views), so a single batch
feeds both the referential-integrity checks (PROP-RI) and the CDC-consistency
checks (CDC-1..7). Curves are renormalized to mean 1.0 (so ``target_tps`` is the
exact daily average), which keeps the funnel/latency *rates* a function of the
manifest + seed only — STAT-F/L assert those, STAT-SHAPE asserts the curve shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.manifest import merge_overlay, parse_manifest_text
from generation.infra.clock import DeterministicWallClock

# The builtin FULL manifest this phase registers as 1.1.0 (catalog/builtin/...).
_BUILTIN_FULL = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.1.0.yaml"
)

# Pins shared with the subset harness so the two determinism units are comparable.
VIRTUAL_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
WALL_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# A 30-simulated-day window — the STAT-SHAPE backfill horizon and large enough that
# a 1M batch never runs out of timers (PRD §4.3 / testing-strategy § STAT).
SIMULATED_DAYS = 30
_US_PER_DAY = 86_400 * 1_000_000
WORKSPACE_ID = "0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60"
STREAM_ID = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"

# Default batch-driver behavioural rates (matched to the subset harness so the
# determinism unit is fully specified for GOLD-B; L3 estimates override these in
# production).
_MEAN_EVENTS_PER_SESSION = 5.0
_VISITS_PER_ACTOR_DAY = 1.0


@lru_cache(maxsize=1)
def _builtin_full_text() -> str:
    return _BUILTIN_FULL.read_text(encoding="utf-8")


def full_ecommerce_document(overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse + overlay the builtin FULL manifest 1.1.0 (pure, no Django)."""
    document = parse_manifest_text(_builtin_full_text())
    return merge_overlay(document, overlay or {})


@dataclass(frozen=True)
class FullBatchResult:
    """A full-manifest canonical batch + the seeded position-0 reference set.

    ``seeded_keys`` is every pool key present at seed time (the position-0 reference
    frame for PROP-RI-1 — pool-seeded entities have no preceding envelope this phase
    because snapshot ``r`` rows ride the *stream head*, not the batch driver). The
    CDC ``c``/``u`` rows are interleaved in ``sequence_no`` order inside
    ``envelopes`` (R-CDC-2 adjacency), exactly as a consumer would read them.
    """

    envelopes: list[Any]
    seeded_keys: dict[str, set[str]]  # entity_type -> set of seeded pool keys
    seed: int
    overlay: dict[str, Any]


def build_full_batch(
    *,
    seed: int,
    max_events: int | None,
    overlay: dict[str, Any] | None = None,
    simulated_days: int = SIMULATED_DAYS,
    pass_size: int = 500,
    arrival_until_us: int | None = None,
) -> FullBatchResult:
    """Drive the generic engine over the FULL manifest to ``max_events`` events.

    Identical mechanics to :func:`tests.golden.harness.build_batch` (the subset
    harness) but pinned to the 1.1.0 full manifest, so CDC + curves are exercised.
    ``pass_size`` is exposed for the determinism-boundary check (content must not
    depend on pass boundaries, behavior-engine §7.4); GOLD-B pins the default.
    """
    document = full_ecommerce_document(overlay)
    ir = compile_manifest(document)
    config = ShardConfig(
        seed=seed,
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        shard_id=0,
        virtual_epoch=VIRTUAL_EPOCH,
        mode="backfill",
        mean_events_per_session=_MEAN_EVENTS_PER_SESSION,
        visits_per_actor_day=_VISITS_PER_ACTOR_DAY,
    )
    clock = DeterministicWallClock(epoch=WALL_EPOCH)
    shard = Shard(ir, config, clock)
    head = shard.seed()
    seeded_keys: dict[str, set[str]] = {}
    for entity_type in ir.entity_order:
        seeded_keys[entity_type] = set(shard.pools.pool(entity_type).records)
    rest = shard.run_batch(
        max_events=max_events,
        until_us=simulated_days * _US_PER_DAY,
        pass_size=pass_size,
        arrival_until_us=arrival_until_us,
    )
    return FullBatchResult(
        envelopes=[*head, *rest],
        seeded_keys=seeded_keys,
        seed=seed,
        overlay=overlay or {},
    )
