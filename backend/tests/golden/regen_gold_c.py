"""Regenerate the committed GOLD-C chaos fixture (local use only — never in CI).

Run via ``uv run python -m tests.golden.regen_gold_c`` (or ``make golden-regen-c``).
Writes the GOLD-C determinism unit — the all-7-modes chaos projection over a
5,000-event canonical batch at ``SEED_GOLD_C`` — to
``backend/tests/golden/chaos/gold-c-5k/``:

* ``delivered.jsonl.gz`` — the post-chaos delivery stream, one envelope per line,
  gzip ``mtime=0`` so the bytes are reproducible;
* ``injections.jsonl.gz`` — the sorted CHD-1 injection projection (wall-clock
  fields dropped), one JSON row per line;
* ``meta.json`` — the PIN-1 determinism unit ``(config sha256, seed, delivered
  count, injection count, envelope_version)`` plus a SHA-256 of each blob.

Re-baselining policy (testing-strategy §6) matches GOLD-A/B: regenerating is
allowed only in a ``golden-rebaseline``-labelled PR that explains the intentional
change. **CI never runs this script** — the GOLD-C replay suite only *reads* the
committed fixture and asserts byte-identity (CHD-2, Phase 9 exit #2).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from dataforge_engine.envelope import ENVELOPE_VERSION
from tests.golden.harness_gold_c import (
    GOLD_C_EVENTS,
    blob_sha256,
    config_sha256,
    delivered_lines,
    injection_lines,
)
from tests.seeds import SEED_GOLD_C

GOLD_C_DIR = Path(__file__).resolve().parent / "chaos" / "gold-c-5k"
DELIVERED_FILE = GOLD_C_DIR / "delivered.jsonl.gz"
INJECTIONS_FILE = GOLD_C_DIR / "injections.jsonl.gz"
META_FILE = GOLD_C_DIR / "meta.json"


def _write_gz(path: Path, lines: list[bytes]) -> None:
    blob = b"\n".join(lines) + b"\n"
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as gz:
        gz.write(blob)


def write_gold_c() -> Path:
    """(Re)write the committed GOLD-C fixture; return its directory."""
    GOLD_C_DIR.mkdir(parents=True, exist_ok=True)
    delivered = delivered_lines()
    injections = injection_lines()
    _write_gz(DELIVERED_FILE, delivered)
    _write_gz(INJECTIONS_FILE, injections)
    meta = {
        "fixture": "gold-c-5k",
        "modes": "all-7",
        "config_sha256": config_sha256(),
        "seed": SEED_GOLD_C,
        "canonical_events": GOLD_C_EVENTS,
        "delivered_count": len(delivered),
        "injection_count": len(injections),
        "envelope_version": ENVELOPE_VERSION,
        "delivered_sha256": blob_sha256(delivered),
        "injections_sha256": blob_sha256(injections),
    }
    META_FILE.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return GOLD_C_DIR


if __name__ == "__main__":  # pragma: no cover - local CLI
    out = write_gold_c()
    print(f"GOLD-C fixture regenerated at {out} ({GOLD_C_EVENTS} canonical events)")
    print("Re-baselining requires the 'golden-rebaseline' PR label (testing-strategy §6).")
