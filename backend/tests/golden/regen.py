"""Regenerate the committed GOLD-A fixture (local use only — never in CI).

Run via ``make golden-regen`` (or ``uv run python -m tests.golden.regen``). Writes
the GOLD-A determinism unit to ``backend/tests/golden/ecommerce/1.0.0/gold-a-1k/``:

* ``events.jsonl.gz`` — the 1,000-event canonical batch (event-model S-2), one
  envelope per line, gzip with ``mtime=0`` so the bytes are reproducible;
* ``meta.json`` — the PIN-1 determinism unit ``(scenario_slug, manifest_version,
  config sha256, seed, event_count, envelope_version)`` plus a SHA-256 of the
  uncompressed JSONL so a reviewer can eyeball that the payload actually changed.

Re-baselining policy (testing-strategy §6): regenerating a golden is allowed only
in a PR that (a) explains which intentional change altered the determinism unit's
output, (b) updates this metadata, and (c) is labelled ``golden-rebaseline`` for
reviewer attention. **CI never runs this script** — the replay suite only *reads*
the committed fixture and asserts byte-identity. This script is the single, local,
auditable way to move the baseline.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from dataforge_engine.envelope import ENVELOPE_VERSION, canonical_serialize
from tests.golden.harness import (
    SIMULATED_DAYS,
    VIRTUAL_EPOCH,
    WALL_EPOCH,
    build_batch,
    merged_ecommerce_document,
)
from tests.seeds import SEED_GOLD_A  # the seed registry is pure (no Django)

GOLD_A_EVENTS = 1_000
GOLD_A_DIR = Path(__file__).resolve().parent / "ecommerce" / "1.0.0" / "gold-a-1k"
EVENTS_FILE = GOLD_A_DIR / "events.jsonl.gz"
META_FILE = GOLD_A_DIR / "meta.json"


def _config_sha256(document: dict[str, object]) -> str:
    canonical = json.dumps(document, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def gold_a_lines() -> list[bytes]:
    """Produce the GOLD-A canonical batch as serialized JSONL byte-lines."""
    result = build_batch(seed=SEED_GOLD_A, max_events=GOLD_A_EVENTS)
    return [canonical_serialize(env) for env in result.envelopes]


def write_gold_a() -> Path:
    """(Re)write the committed GOLD-A fixture; return its directory."""
    GOLD_A_DIR.mkdir(parents=True, exist_ok=True)
    lines = gold_a_lines()
    blob = b"\n".join(lines) + b"\n"
    with gzip.GzipFile(filename=str(EVENTS_FILE), mode="wb", mtime=0) as gz:
        gz.write(blob)
    document = merged_ecommerce_document()
    meta = {
        "fixture": "gold-a-1k",
        "scenario_slug": document["metadata"]["slug"],
        "manifest_version": document["metadata"]["version"],
        "config_sha256": _config_sha256(document),
        "seed": SEED_GOLD_A,
        "event_count": len(lines),
        "envelope_version": ENVELOPE_VERSION,
        "virtual_epoch": VIRTUAL_EPOCH.isoformat(),
        "wall_epoch": WALL_EPOCH.isoformat(),
        "wall_clock": "DeterministicWallClock(step=1ms/event)",
        "simulated_days": SIMULATED_DAYS,
        "jsonl_sha256": hashlib.sha256(blob).hexdigest(),
    }
    META_FILE.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return GOLD_A_DIR


if __name__ == "__main__":  # pragma: no cover - local CLI
    out = write_gold_a()
    print(f"GOLD-A fixture regenerated at {out} ({GOLD_A_EVENTS} events)")
    print("Re-baselining requires the 'golden-rebaseline' PR label (testing-strategy §6).")
