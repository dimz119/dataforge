"""The shared pure-engine batch harness for the GOLD + PROP suites (Phase 4).

Both the golden-replay suite (§6) and the referential-integrity property suite
(§4.1) drive the *same* generic engine over the builtin ``ecommerce`` subset
manifest with a **deterministic injected wall clock**, then assert over the
produced canonical envelopes. This module is the single place that:

* loads + compiles the builtin manifest **without Django** (the engine front-end
  ``parse_manifest_text`` + ``merge_overlay`` are framework-free), so the GOLD and
  PROP lanes run in the fast pure-engine lane — no Postgres, no Redis;
* builds a backfill :class:`~dataforge_engine.behavior.Shard` at a pinned seed,
  virtual epoch, and a 1-ms-per-event :class:`DeterministicWallClock` so the full
  envelope — wall ``emitted_at`` included — is byte-stable across runs (GOLD-A);
* captures the **seeded entity-pool keys** at seed time. This phase emits no
  snapshot ``op:"r"`` rows (Phase 8), so a payload reference to a pool-seeded
  entity has no preceding event; the seeded keys are therefore the position-0
  reference set the entity-resolution property (PROP-RI-1) must allow (§4.1).

The harness is import-safe with or without ``DJANGO_SETTINGS_MODULE`` set: it
touches only ``dataforge_engine`` (pure) and ``generation.infra.clock`` (a tiny
pure host seam with no Django import).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from dataforge_engine.behavior import Shard, ShardConfig, compile_manifest
from dataforge_engine.envelope import canonical_serialize
from dataforge_engine.manifest import merge_overlay, parse_manifest_text
from generation.infra.clock import DeterministicWallClock

# The builtin subset manifest the engine runs this phase (catalog/builtin/...).
_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)

# A pinned virtual epoch so occurred_at is reproducible (the simulated clock head).
VIRTUAL_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# The deterministic wall-clock epoch the golden harness pins (testing-strategy §6).
WALL_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
# A pinned 30-simulated-day window so a large batch never runs out of timers.
SIMULATED_DAYS = 30
_US_PER_DAY = 86_400 * 1_000_000
WORKSPACE_ID = "0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60"
STREAM_ID = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"

# Default batch-driver behavioural rates (the L3 estimate overrides in production;
# pinned here so the GOLD/PROP determinism unit is fully specified).
_MEAN_EVENTS_PER_SESSION = 5.0
_VISITS_PER_ACTOR_DAY = 1.0


@lru_cache(maxsize=1)
def _builtin_text() -> str:
    return _BUILTIN.read_text(encoding="utf-8")


def merged_ecommerce_document(overlay: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse + overlay the builtin subset manifest (pure, no Django)."""
    document = parse_manifest_text(_builtin_text())
    return merge_overlay(document, overlay or {})


@dataclass(frozen=True)
class BatchResult:
    """The produced canonical batch + the seeded position-0 reference set."""

    envelopes: list[Any]
    seeded_keys: dict[str, set[str]]  # entity_type -> set of seeded pool keys
    seed: int
    overlay: dict[str, Any]


def build_batch(
    *,
    seed: int,
    max_events: int,
    overlay: dict[str, Any] | None = None,
    simulated_days: int = SIMULATED_DAYS,
    pass_size: int = 500,
) -> BatchResult:
    """Drive the generic engine to ``max_events`` under the deterministic wall clock.

    Returns the produced :class:`InternalEnvelope` list (CDC rows interleaved in
    ``sequence_no`` order) and the seeded entity-pool keys captured at seed time —
    everything the GOLD byte-identity and the PROP referential checks need.

    ``pass_size`` is exposed so the determinism-boundary test can prove canonical
    content is independent of pass boundaries (behavior-engine §7.4); the golden
    fixture is generated with the default to pin a single byte sequence.
    """
    document = merged_ecommerce_document(overlay)
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
    # Seed first so the pools exist, then snapshot the keys as the position-0 set.
    # ``Shard.seed`` is idempotent (the ``_seeded`` flag), so the subsequent
    # ``run_batch`` does not re-seed — it returns an empty head and drains the heap.
    head = shard.seed()
    seeded_keys: dict[str, set[str]] = {}
    for entity_type in ir.entity_order:
        seeded_keys[entity_type] = set(shard.pools.pool(entity_type).records)
    rest = shard.run_batch(
        max_events=max_events, until_us=simulated_days * _US_PER_DAY, pass_size=pass_size
    )
    return BatchResult(
        envelopes=[*head, *rest],
        seeded_keys=seeded_keys,
        seed=seed,
        overlay=overlay or {},
    )


def content_only(envelope: Any) -> str:
    """A canonical-content projection of an envelope — wall-domain fields removed.

    Strips ``emitted_at`` and the CDC ``ts_ms`` / ``source.ts_ms`` echoes (the only
    wall-domain fields; ``occurred_at`` and the CDC ``__now__``/``created_at``/
    ``updated_at`` are virtual-clock derived and therefore content). Used by the
    determinism-boundary test to prove content is invariant to pass boundaries
    (behavior-engine §7.4: content must never vary with pass sizes/tick boundaries)."""
    obj: dict[str, Any] = json.loads(canonical_serialize(envelope))
    obj.pop("emitted_at", None)
    payload = obj.get("payload")
    if isinstance(payload, dict):
        payload.pop("ts_ms", None)
        source = payload.get("source")
        if isinstance(source, dict):
            source.pop("ts_ms", None)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
