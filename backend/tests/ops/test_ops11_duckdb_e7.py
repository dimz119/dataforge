"""OPS-11 — the DuckDB / Exercise-E7 round-trip (testing-strategy §11; exit #3).

Generates a 100,000-event delivered-shape JSONL dataset from the generic engine
(the same canonical batch a real ``GET /datasets/{id}/download`` would stream,
``_df`` stripped → the 20-key envelope), then drives the **published** E7 DuckDB
assertions via ``infra/scripts/e7_duckdb_assert.py`` — the exact harness the
phase demo (step 8) and the exercise doc walk a learner through. Asserts:

  * 100,000 rows load (``read_json_auto``);
  * orders→users FK join match = 100 % (referential integrity survives the
    round-trip to an analytics engine, INV-GEN-1 end-to-end);
  * the daily-revenue query returns rows.

Pure engine + DuckDB — no Postgres/Redis — but it carries the ``ops`` marker
because it is a heavier round-trip (≈ 100k-event generation + a DuckDB load) that
belongs in the merge/integration lane, not the per-PR unit lane (testing-strategy
§14 Phase-4 OPS-11 row → merge lane).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from dataforge_engine.envelope import canonical_serialize_str, strip_internal
from tests.golden.harness import build_batch
from tests.seeds import SEED_GOLD_A

pytestmark = pytest.mark.ops

OPS11_EVENTS = 100_000
_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "scripts"
    / "e7_duckdb_assert.py"
)


def _load_e7_module() -> object:
    """Import the standalone E7 assertion script (it lives under infra/scripts)."""
    spec = importlib.util.spec_from_file_location("e7_duckdb_assert", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e7_duckdb_assert"] = module
    spec.loader.exec_module(module)
    return module


def _write_dataset_jsonl(path: Path) -> int:
    """Write a 100k-event delivered-shape JSONL (the dataset download shape)."""
    result = build_batch(seed=SEED_GOLD_A, max_events=OPS11_EVENTS)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for env in result.envelopes:
            fh.write(canonical_serialize_str(strip_internal(env)) + "\n")
            count += 1
    return count


def test_ops11_duckdb_e7_round_trip(tmp_path: Path) -> None:
    """OPS-11: a 100k dataset loads into DuckDB and passes all three E7 assertions."""
    pytest.importorskip("duckdb")
    dataset = tmp_path / "ops11.jsonl"
    written = _write_dataset_jsonl(dataset)
    assert written == OPS11_EVENTS, f"expected {OPS11_EVENTS} events, wrote {written}"

    e7 = _load_e7_module()
    metrics = e7.run_e7(dataset, expect_rows=OPS11_EVENTS)  # type: ignore[attr-defined]

    assert metrics["rows"] == OPS11_EVENTS
    assert metrics["orders"] > 0
    assert metrics["orders_matched"] == metrics["orders"], "orders→users FK join < 100%"
    assert metrics["revenue_days"] >= 1
