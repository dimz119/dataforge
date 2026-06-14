"""Layer-3 dry-run (plugin-arch §8.4) — the catalog-side host + GUARD coverage.

Proves the four behaviours the Phase-4 L3 deliverable contracts:

1. A manifest with a near-absorbing **stay** loop that passes L1+L2 trips MAN-D602
   in the dry run (the demo livelock case) — and is *bounded*, not hung (the
   10,000-transition hard cap is hit per traversal, the dry run returns).
2. The builtin ecommerce manifest passes L3 with ``est_eps_per_shard >= 1000``
   (exit criterion #4; the same check the CI GUARD enforces).
3. The §8.4 caps are enforced: a runaway manifest is bounded, the result carries
   the §8.3 ``dry_run`` block, and the merged report downgrades to ``failed``.
4. Determinism: the fixed sandbox seed reproduces the realized content metrics
   (``mean_events_per_session`` etc.) across runs.

These are pure-facade tests (no DB) over ``catalog.application.dry_run`` +
``dataforge_engine.behavior.run_dry_run``; the persisted-row orchestration
(``validation_l3``) and the Celery task are covered in ``test_dry_run_l3_db.py``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from catalog.application import dry_run
from dataforge_engine.behavior import EPS_FLOOR, run_dry_run
from dataforge_engine.manifest import ValidationReport, validate_manifest

_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)


def _builtin_document() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(_BUILTIN.read_text(encoding="utf-8"))
    return document


def _near_absorbing_livelock() -> dict[str, Any]:
    """A manifest that passes L1+L2 but livelocks at runtime (the MAN-D602 demo).

    V207 sees the guarded ``done`` transition's 0.95 mass *escaping* to a terminal
    (guards treated as passing), so expected-steps is tiny and L1+L2 pass. V206
    does not fire because the second transition (the self-loop) is unguarded. But
    at runtime the guard ``actor.tier == 'impossible'`` never passes — selection
    falls through to ``remainder: stay`` and re-enters ``spinning`` forever, and
    the self-loop also re-enters: every path re-enters with PT0S dwell, so virtual
    time never advances past the session timeout and the traversal hits the
    10,000-transition hard cap → MAN-D602 (guard-induced livelock V205/V207 could
    not see).
    """
    return copy.deepcopy(_LIVELOCK)


_LIVELOCK: dict[str, Any] = {
    "manifest_schema": "v0",
    "metadata": {
        "slug": "livelock",
        "version": "1.0.0",
        "title": "Livelock",
        "actor_entity": "users",
    },
    "entities": {
        "users": {
            "key_prefix": "usr",
            "key_attribute": "user_id",
            "attributes": {
                "tier": {"generator": "choice.uniform", "params": {"options": ["free"]}},
            },
        },
    },
    "event_types": {
        "noop": {"payload": {"user_id": {"from": "actor.user_id"}}},
    },
    "state_machines": {
        "shopping_session": {
            "type": "session",
            "binds": "users",
            "initial": "spinning",
            "states": {
                "spinning": {
                    "remainder": "stay",
                    "transitions": [
                        {
                            "to": "done",
                            "probability": 0.95,
                            "guard": {
                                "all": [
                                    {"path": "actor.tier", "op": "eq", "value": "impossible"},
                                ]
                            },
                        },
                        {"to": "spinning", "probability": 0.04, "emit": "noop"},
                    ],
                },
                "done": {"terminal": True},
            },
        },
    },
    "seeding": {"catalogs": {"users": {"default": 100, "min": 1, "max": 1000}}},
}


# --- 1. the near-absorbing stay-loop → MAN-D602 -----------------------------


def test_livelock_passes_l1_l2_but_trips_d602() -> None:
    document = _near_absorbing_livelock()
    # Static L1+L2 cannot see the guard-induced livelock.
    l1l2 = validate_manifest(document)
    assert l1l2.passed, l1l2.codes()

    result = run_dry_run(document)
    assert not result.passed
    codes = [e[0] for e in result.errors]
    assert "MAN-D602" in codes


def test_livelock_is_bounded_not_hung() -> None:
    # The dry run returns (the per-traversal hard cap bounds the runaway); if it
    # hung this test would time out. We additionally assert the sandbox stopped it
    # rather than completing 1,000 sessions.
    result = run_dry_run(_near_absorbing_livelock())
    assert result.metrics["traversals_completed"] < 1_000
    assert "MAN-D602" in [e[0] for e in result.errors]


def test_d602_merges_into_report_and_downgrades_status() -> None:
    document = _near_absorbing_livelock()
    base = validate_manifest(document)
    assert base.passed
    result = run_dry_run(document)
    merged = dry_run.merge_dry_run_into_report(base, result)
    assert merged.status == "failed"
    assert "MAN-D602" in [e.code for e in merged.errors]
    # The §8.3 dry_run block is attached even on a failing run.
    assert merged.dry_run is not None
    assert "events_generated" in merged.dry_run


# --- 2. the builtin passes L3 with est_eps_per_shard >= 1000 ----------------


def test_builtin_ecommerce_passes_l3_with_throughput_floor() -> None:
    document = _builtin_document()
    result = run_dry_run(document)
    assert result.passed, [e[0] for e in result.errors]
    assert result.est_eps_per_shard >= EPS_FLOOR
    # The realized-behaviour metrics the behaviour engine consumes are present.
    assert result.metrics["mean_events_per_session"] > 0
    assert result.metrics["traversals_completed"] >= 1


def test_builtin_merged_report_is_passing_with_dry_run_block() -> None:
    document = _builtin_document()
    base = validate_manifest(document)
    assert base.passed
    merged = dry_run.merge_dry_run_into_report(base, run_dry_run(document))
    assert merged.status == "passed"
    assert merged.dry_run is not None
    assert merged.dry_run["est_eps_per_shard"] >= EPS_FLOOR
    # The §8.3 shape round-trips through to_dict.
    wire = merged.to_dict()
    assert wire["status"] == "passed"
    assert wire["dry_run"]["est_eps_per_shard"] >= EPS_FLOOR


# --- 3. caps + the run-on-report sequencing seam ----------------------------


def test_run_layer3_on_report_skips_when_l1_l2_failed() -> None:
    # L3 must not run on a structurally invalid document (§8.4 sequencing).
    failed_base = {"status": "failed", "errors": [{"code": "MAN-V201"}], "warnings": []}
    out = dry_run.run_layer3_on_report(_builtin_document(), failed_base)
    assert out is failed_base  # returned unchanged, no dry_run added
    assert "dry_run" not in out


def test_run_layer3_on_report_attaches_dry_run_on_passing_base() -> None:
    document = _builtin_document()
    base = validate_manifest(document).to_dict()
    out = dry_run.run_layer3_on_report(document, base)
    assert out["status"] == "passed"
    assert out["dry_run"]["est_eps_per_shard"] >= EPS_FLOOR


# --- 4. determinism (the fixed sandbox seed) --------------------------------


def test_dry_run_content_metrics_are_deterministic() -> None:
    document = _builtin_document()
    first = run_dry_run(document)
    second = run_dry_run(document)
    # Content-derived metrics are a pure function of (manifest, sandbox seed) and
    # must be byte-identical across runs; only est_eps_per_shard (wall throughput)
    # may differ between runs.
    for key in (
        "events_generated",
        "traversals_completed",
        "mean_events_per_session",
        "max_payload_bytes",
        "p99_payload_bytes",
        "realized_rates",
    ):
        assert first.metrics[key] == second.metrics[key], key


def test_merge_with_empty_metrics_yields_null_dry_run() -> None:
    # A compile-error result carries no metrics; the merged report's dry_run is null.
    base = ValidationReport(status="passed")
    empty = dry_run.DryRunResult(metrics={}, errors=[], warnings=[])
    merged = dry_run.merge_dry_run_into_report(base, empty)
    assert merged.dry_run is None
