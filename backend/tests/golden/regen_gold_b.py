"""Regenerate the committed GOLD-B fixture (local use only — never in CI).

Run via ``make golden-regen-b`` (or ``uv run python -m tests.golden.regen_gold_b``).
Writes the GOLD-B determinism unit — the **full** manifest (ecommerce 1.1.0) with
CDC + intensity curves, 10,000 events at ``SEED_GOLD_B`` — to
``backend/tests/golden/ecommerce/1.1.0/gold-b-10k/``:

* ``events.jsonl.gz`` — the 10,000-event canonical batch (CDC ``c``/``u`` rows
  interleaved in ``sequence_no`` order), one envelope per line, gzip ``mtime=0`` so
  the bytes are reproducible;
* ``meta.json`` — the PIN-1 determinism unit ``(scenario_slug, manifest_version,
  config sha256, seed, event_count, envelope_version)`` plus a SHA-256 of the
  uncompressed JSONL.

Re-baselining policy (testing-strategy §6) is identical to GOLD-A: regenerating is
allowed only in a PR that explains the intentional change, updates this metadata,
and is labelled ``golden-rebaseline``. **CI never runs this script** — the GOLD-B
replay suite only *reads* the committed fixture and asserts byte-identity. This
proves determinism survives CDC + curves (phase-08 exit criterion #6).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from dataforge_engine.envelope import ENVELOPE_VERSION, canonical_serialize
from tests.golden.harness_full import (
    SIMULATED_DAYS,
    VIRTUAL_EPOCH,
    WALL_EPOCH,
    build_full_batch,
    full_ecommerce_document,
)
from tests.seeds import SEED_GOLD_B  # the seed registry is pure (no Django)

GOLD_B_EVENTS = 10_000
GOLD_B_DIR = Path(__file__).resolve().parent / "ecommerce" / "1.1.0" / "gold-b-10k"
EVENTS_FILE = GOLD_B_DIR / "events.jsonl.gz"
META_FILE = GOLD_B_DIR / "meta.json"


def _config_sha256(document: dict[str, object]) -> str:
    canonical = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def gold_b_lines() -> list[bytes]:
    """Produce the GOLD-B canonical batch as serialized JSONL byte-lines."""
    result = build_full_batch(seed=SEED_GOLD_B, max_events=GOLD_B_EVENTS)
    return [canonical_serialize(env) for env in result.envelopes]


def write_gold_b() -> Path:
    """(Re)write the committed GOLD-B fixture; return its directory."""
    GOLD_B_DIR.mkdir(parents=True, exist_ok=True)
    lines = gold_b_lines()
    blob = b"\n".join(lines) + b"\n"
    with gzip.GzipFile(filename=str(EVENTS_FILE), mode="wb", mtime=0) as gz:
        gz.write(blob)
    document = full_ecommerce_document()
    meta = {
        "fixture": "gold-b-10k",
        "scenario_slug": document["metadata"]["slug"],
        "manifest_version": document["metadata"]["version"],
        "config_sha256": _config_sha256(document),
        "seed": SEED_GOLD_B,
        "event_count": len(lines),
        "envelope_version": ENVELOPE_VERSION,
        "virtual_epoch": VIRTUAL_EPOCH.isoformat(),
        "wall_epoch": WALL_EPOCH.isoformat(),
        "wall_clock": "DeterministicWallClock(step=1ms/event)",
        "simulated_days": SIMULATED_DAYS,
        "jsonl_sha256": hashlib.sha256(blob).hexdigest(),
    }
    META_FILE.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return GOLD_B_DIR


if __name__ == "__main__":  # pragma: no cover - local CLI
    out = write_gold_b()
    print(f"GOLD-B fixture regenerated at {out} ({GOLD_B_EVENTS} events)")
    print("Re-baselining requires the 'golden-rebaseline' PR label (testing-strategy §6).")
