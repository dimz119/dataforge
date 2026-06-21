"""The GOLD-C chaos determinism unit — all-7-modes over a canonical batch (§10.1).

GOLD-C is the chaos sibling of GOLD-A/B: the deterministic chaos projection (the
pure :class:`ChaosPipeline` over a generated canonical batch, no broker, no DB)
with **all seven modes enabled** at ``SEED_GOLD_C``, frozen to two committed
artifacts under ``backend/tests/golden/chaos/gold-c-5k/``:

* ``delivered.jsonl.gz`` — the post-chaos delivery stream (in-line instances:
  survivors + duplicate copies + corrupted/nulled/drifted payloads + the
  out-of-order shuffle; late-extracted instances leave the in-line flow, §5.7),
  one canonically-serialized internal envelope per line;
* ``injections.jsonl.gz`` — the answer-key injection projection: the CHD-1
  deterministic projection of every :class:`InjectionRecord` (wall-clock fields
  dropped), one JSON object per line, sorted, so the byte-identity is over chaos
  DECISIONS not wall artifacts.

``build_gold_c()`` is a thin wrapper over :func:`tests.chaos.projection.run_projection`
pinned to ``SEED_GOLD_C`` so the regen script and the replay test produce the
exact same bytes (CHD-2 byte-identity). Pure engine + ports — fast golden lane.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from dataforge_engine.chaos import ChaosPolicy
from tests.chaos.projection import (
    all_modes_policy,
    deterministic_projection,
    run_projection,
)
from tests.seeds import SEED_GOLD_C

GOLD_C_EVENTS = 5000
# The fixed all-7-modes config GOLD-C pins (rate 0.10; a fixed late delay so the
# due_at arithmetic is byte-stable). This IS the GOLD-C determinism unit's config.
_RATE = 0.10


def gold_c_policy() -> ChaosPolicy:
    """The frozen all-7-modes GOLD-C policy (the committed determinism config).

    Delegates to :func:`tests.chaos.projection.all_modes_policy` at the GOLD-C rate
    (0.10) — all seven modes on, a fixed late delay so the due_at arithmetic is
    byte-stable. This IS the GOLD-C determinism unit's config (config_sha256)."""
    return all_modes_policy(_RATE)


def build_gold_c() -> Any:
    """Run the deterministic chaos projection at ``SEED_GOLD_C`` (the GOLD-C unit).

    The projection's chaos sub-seed is fixed inside ``run_projection``; the seed
    registry value ``SEED_GOLD_C`` is the *fixture* seed of record (PIN-1) and is
    asserted into the meta so the determinism unit is fully specified.
    """
    return run_projection(gold_c_policy(), n=GOLD_C_EVENTS)


def _stable_json(value: Any) -> bytes:
    """Deterministic JSON bytes (sorted keys, compact). Used for GOLD-C lines.

    The post-chaos delivered stream deliberately carries schema-violating values
    (e.g. ``corrupted_values``' ``int_overflow`` > 2^53, S-1) that the canonical
    envelope serializer rejects by design — so GOLD-C serializes the delivered
    instances with a plain stable JSON dump, which still gives byte-identity over
    the chaos decisions (CHD-2) without re-imposing the clean-envelope contract.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def delivered_lines() -> list[bytes]:
    """The delivered post-chaos stream as stable-JSON JSONL byte-lines."""
    projection = build_gold_c()
    return [_stable_json(env) for env in projection.delivered]


def injection_lines() -> list[bytes]:
    """The sorted CHD-1 injection projection as JSON byte-lines (wall-clock free)."""
    projection = build_gold_c()
    rows = deterministic_projection(projection.records)
    return [json.dumps(row, sort_keys=True, default=str).encode("utf-8") for row in rows]


def config_sha256() -> str:
    """SHA-256 of the canonicalized GOLD-C policy (the PIN-1 config identity)."""
    canonical = json.dumps(gold_c_policy(), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def blob_sha256(lines: list[bytes]) -> str:
    return hashlib.sha256(b"\n".join(lines) + b"\n").hexdigest()


GOLD_C_SEED = SEED_GOLD_C
