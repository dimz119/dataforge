"""Layer-2 resource-bound tests (MAN-V304/V305/V308/V312/V314/V315).

The per-object counts (B-03/B-06/B-07…) are enforced structurally by the §9.1
Layer-1 schema; these tests cover the aggregate / cross-cutting bounds Layer 2
owns, asserting {code, bound, actual}.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.manifest import validate_manifest

from .fixtures import valid_subset_manifest


def _errs(doc: dict[str, Any], code: str) -> list[Any]:
    return [e for e in validate_manifest(doc).errors if e.code == code]


def _l2_errs(doc: dict[str, Any], code: str) -> list[Any]:
    """Run Layer 2 directly (bypassing L1) for bounds unreachable via an L1-valid doc."""
    from dataforge_engine.manifest import ErrorCollector, run_layer2

    collector = ErrorCollector()
    run_layer2(doc, collector)
    return [e for e in collector.errors if e.code == code]


def test_man_v304_total_attributes_exceeded() -> None:
    """B-04 aggregate: Σ attributes > 2000 via the L2 walk (L1 caps 100/entity)."""
    doc = valid_subset_manifest()
    # 25 entities x 90 attrs = 2250 > 2000; each entity stays under the L1 cap.
    doc["entities"] = {
        f"e{i}": {
            "key_prefix": "ab",
            "key_attribute": "k",
            "attributes": {f"a{j}": {"generator": "text.word"} for j in range(90)},
        }
        for i in range(25)
    }
    errs = _l2_errs(doc, "MAN-V304")
    assert errs
    assert errs[0].bound == 2000
    assert errs[0].actual == 2250


def test_man_v305_subjects_exceeded_via_l2() -> None:
    """B-05 aggregate: business + cdc subjects > 250.

    200 event types (exactly the event-type cap) + 51 cdc entities = 251 derived
    subjects > 250. The subject-total finding is the one with bound 250.
    """
    doc = valid_subset_manifest()
    doc["event_types"] = {
        f"evt_{i}": {"payload": {"x": {"const": "y"}}} for i in range(200)
    }
    doc["cdc"] = {"entities": {f"e{i}": {"enabled_default": True} for i in range(51)}}
    subject_errs = [e for e in _l2_errs(doc, "MAN-V305") if e.bound == 250]
    assert subject_errs
    assert subject_errs[0].actual == 251


def test_man_v312_entity_refs_exceeded() -> None:
    """B-12: derived entity_refs per event > 16 (R-EVT-5) via distinct created refs."""
    doc = valid_subset_manifest()
    payload = {
        f"f{i}": {"from": f"created.ent{i}.x"} for i in range(17)
    }
    doc["event_types"]["order_placed"]["payload"] = payload
    errs = _l2_errs(doc, "MAN-V312")
    assert errs
    assert errs[0].bound == 16
    assert errs[0].actual == 17


def test_man_v308_seed_default_below_min() -> None:
    doc = valid_subset_manifest()
    doc["seeding"]["catalogs"]["users"]["default"] = 10  # below min 100
    errs = _errs(doc, "MAN-V308")
    assert errs
    assert errs[0].bound == 100
    assert errs[0].actual == 10


def test_man_v308_actor_default_zero() -> None:
    doc = valid_subset_manifest()
    doc["seeding"]["catalogs"]["users"] = {"default": 0}
    errs = _errs(doc, "MAN-V308")
    assert any(e.bound == 1 and e.actual == 0 for e in errs)


def test_man_v314_document_total_exceeded() -> None:
    """Exceed B-14's document total (20) by exceeding the per-entity cap removal.

    The §9.1 schema caps maxItems:8 per entity, so 20+ across two entities is
    structurally impossible; the document-total check is still meaningful for
    manifests with many CDC entities. Construct three CDC entities each at 7.
    """
    doc = valid_subset_manifest()
    doc["entities"]["payments"] = {
        "key_prefix": "pay",
        "key_attribute": "payment_id",
        "attributes": {"amount": {"generator": "number.decimal",
                                  "params": {"min": "1.00", "max": "9.00"}}},
    }
    mut = {
        "name": "m",
        "rate": {"per": "entity_day", "probability": 0.01},
        "set": {"amount": {"generator": "number.decimal",
                           "params": {"min": "1.00", "max": "9.00"}}},
    }
    for ename in ("users", "orders", "payments"):
        doc["cdc"]["entities"].setdefault(ename, {"enabled_default": True})
        doc["cdc"]["entities"][ename]["background_mutations"] = [
            {**mut, "name": f"{ename}{i}"} for i in range(7)
        ]
    # users(7)+orders(7)+payments(7) = 21 > 20.
    errs = _errs(doc, "MAN-V314")
    assert errs
    assert errs[0].bound == 20
    assert errs[0].actual == 21


def test_man_v315_duration_exceeds_year() -> None:
    doc = valid_subset_manifest()
    doc["state_machines"]["shopping_session"]["session_timeout"] = "P400D"
    errs = _errs(doc, "MAN-V315")
    assert errs
    assert errs[0].bound == 365


def test_man_v315_intensity_coverage_gap() -> None:
    doc = valid_subset_manifest()
    doc["intensity"] = {
        "diurnal": [
            {"from_hour": 0, "to_hour": 6, "multiplier": 0.3},
            {"from_hour": 8, "to_hour": 24, "multiplier": 1.0},  # gap 6..8
        ]
    }
    errs = _errs(doc, "MAN-V315")
    assert errs


def test_valid_intensity_passes() -> None:
    doc = valid_subset_manifest()
    doc["intensity"] = {
        "diurnal": [
            {"from_hour": 0, "to_hour": 6, "multiplier": 0.3},
            {"from_hour": 6, "to_hour": 24, "multiplier": 1.0},
        ]
    }
    assert validate_manifest(doc).passed
