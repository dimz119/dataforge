"""CHD-4/5 — delivered-vs-ledger reconciliation + ledger immutability (§10.2).

Phase-9 exit criterion #4 (PR): the answer key counts equal delivered chaos
exactly, to the event, and no chaos run mutates the ledger.

* **CHD-4:** an independent consumer-side tally of every observable deviation —
  duplicate ``event_id``s (extra copies), gaps (suppressed events), mutated/nulled
  fields, drift fields, displaced positions, and late extractions — equals the
  answer-key injection count for that mode exactly (INV-CHA-4). Every delivered
  deviation maps to one injection record and every record maps to a deviation (or
  a late extraction that left the in-line flow).
* **CHD-5:** the ledger's content hash is identical before and after the chaos run
  — chaos transforms the delivery stream, never the ledger (INV-CHA-1).

Pure engine + ports — no broker, no DB; the deterministic projection IS the
consumer-side tally (the in-line delivered stream + the late buffer).
"""

from __future__ import annotations

import pytest

from tests.chaos.projection import (
    all_modes_policy,
    ledger_content_hash,
    run_projection,
)

pytestmark = pytest.mark.chaos

N = 5000


def test_chd5_ledger_content_hash_unchanged_by_any_chaos_run() -> None:
    """CHD-5: the ledger hash is identical before and after an all-7-modes run."""
    proj = run_projection(all_modes_policy(0.20), n=N)
    # The projection returns an independent canonical reference (the immutable
    # ledger); its hash must equal a fresh canonical batch's hash — chaos touched
    # only the delivery stream, never the ledger (INV-CHA-1).
    from dataforge_engine.chaos.tests.fixtures import make_batch

    fresh_ledger = make_batch(N)
    assert ledger_content_hash(proj.ledger) == ledger_content_hash(fresh_ledger)


def test_chd5_ledger_hash_stable_at_max_rate() -> None:
    """CHD-5: even at rate 0.5 (max chaos) the ledger hash is untouched."""
    from dataforge_engine.chaos.tests.fixtures import make_batch

    proj = run_projection(all_modes_policy(0.50), n=2000)
    assert ledger_content_hash(proj.ledger) == ledger_content_hash(make_batch(2000))


def test_chd4_duplicate_copies_reconcile_to_answer_key() -> None:
    """CHD-4: delivered extra copies of an event_id == the duplicates answer key.

    A copy may itself be extracted late (the "redelivered duplicate arrives late"
    shape, CR-2), so the tally spans the in-line stream AND the late buffer."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    extra_copies = sum(
        1
        for e in proj.all_instance_dicts()
        if not e["_df"]["canonical"]
        and (e["_df"]["chaos"] or {}).get("duplicates", {}).get("duplicate_index", 0) >= 1
    )
    assert extra_copies == len(proj.records_for("duplicates"))
    assert extra_copies > 0


def test_chd4_missing_gaps_reconcile_to_answer_key() -> None:
    """CHD-4: ledger event_ids absent from delivery+buffer == the missing answer key."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    delivered_ids = {e["event_id"] for e in proj.delivered}
    buffered_ids = {entry["event_id"] for entry in proj.late_entries}
    ledger_ids = {e["event_id"] for e in proj.ledger}
    # An event is "missing" iff it appears nowhere in delivery truth (in-line or buffer).
    suppressed = ledger_ids - delivered_ids - buffered_ids
    assert len(suppressed) == len(proj.records_for("missing"))
    # Every suppressed id is recorded in the answer key (and present in the ledger).
    missing_ids = {r["event_id"] for r in proj.records_for("missing")}
    assert suppressed == missing_ids


def test_chd4_late_extractions_reconcile_to_answer_key() -> None:
    """CHD-4: every late buffer entry maps to one pending late_arriving record."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    late_records = proj.records_for("late_arriving")
    assert len(proj.late_entries) == len(late_records)
    entry_inj = {entry["injection_id"] for entry in proj.late_entries}
    record_inj = {r["injection_id"] for r in late_records}
    assert entry_inj == record_inj
    assert all(r["details"]["outcome"] == "pending" for r in late_records)


def test_chd4_payload_mutations_reconcile_to_answer_key() -> None:
    """CHD-4: delivered corrupted/nulled/drift fields == those mode answer keys.

    Value modes are keyed per CANONICAL event (CR-1): one injection record per
    event, but every copy carries the identical mutation, so the tally is over
    DISTINCT event_ids across the in-line stream + the late buffer."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    for mode in ("corrupted_values", "nulls", "schema_drift"):
        event_ids = {
            e["event_id"]
            for e in proj.all_instance_dicts()
            if mode in (e["_df"]["chaos"] or {})
        }
        assert len(event_ids) == len(proj.records_for(mode)), f"{mode} mismatch"


def test_chd4_every_record_maps_to_an_observed_deviation() -> None:
    """CHD-4 (reverse): every injection record corresponds to a delivery deviation.

    Full per-mode accounting over the union of delivered + late-extracted
    instances — total observed deviations must equal total injections, with no
    record unattributable and no deviation unrecorded."""
    proj = run_projection(all_modes_policy(0.10), n=N)
    instances = proj.all_instance_dicts()

    def _event_ids_with(mode: str) -> int:
        return len({e["event_id"] for e in instances if mode in (e["_df"]["chaos"] or {})})

    delivered_ids = {e["event_id"] for e in proj.delivered}
    buffered_ids = {x["event_id"] for x in proj.late_entries}
    ledger_ids = {e["event_id"] for e in proj.ledger}

    observed = {
        "missing": len(ledger_ids - delivered_ids - buffered_ids),
        "duplicates": sum(
            1
            for e in instances
            if (e["_df"]["chaos"] or {}).get("duplicates", {}).get("duplicate_index", 0) >= 1
        ),
        "corrupted_values": _event_ids_with("corrupted_values"),
        "nulls": _event_ids_with("nulls"),
        "schema_drift": _event_ids_with("schema_drift"),
        "out_of_order": sum(
            1 for e in instances if "out_of_order" in (e["_df"]["chaos"] or {})
        ),
        "late_arriving": len(proj.late_entries),
    }
    for mode, count in observed.items():
        assert count == len(proj.records_for(mode)), f"{mode}: observed {count}"
    assert sum(observed.values()) == len(proj.records)
