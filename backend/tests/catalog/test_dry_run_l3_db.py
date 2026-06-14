"""Layer-3 dry-run persistence + the builtin-revalidation GUARD (plugin-arch §8.4).

DB-backed coverage of the catalog-side L3 orchestration:

* ``run_layer3_for_version`` loads a persisted ManifestVersion, runs L3 on its
  stored canonical document, and persists the merged §8.3 report onto
  ``validation_report`` (the existing validation endpoint then surfaces the
  ``dry_run`` block) — without touching the immutable manifest (INV-CAT-1).
* The §8.4 sequencing seam: a row whose L1+L2 report has not passed is left
  untouched (L3 skipped).
* The ``revalidate_builtins_l3`` GUARD command passes for the shipped builtins and
  fails (non-zero) if a builtin cannot sustain ``est_eps_per_shard >= 1000``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.application import ingest, validation_l3
from catalog.domain.models import STATUS_DRAFT, ManifestVersion
from tests.catalog.conftest import AuthedWorkspace
from tests.catalog.fixtures import valid_subset_manifest

pytestmark = pytest.mark.django_db

_BUILTIN_DIR = Path(__file__).resolve().parents[2] / "catalog" / "builtin"


def _create_builtin_draft() -> ManifestVersion:
    text = (_BUILTIN_DIR / "ecommerce" / "1.0.0.yaml").read_text(encoding="utf-8")
    return ingest.create_draft(
        text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )


def test_run_layer3_for_version_persists_dry_run_block() -> None:
    draft = _create_builtin_draft()
    assert draft.status == STATUS_DRAFT
    # Before L3 the persisted report has no dry_run block (L1+L2 only).
    assert "dry_run" not in (draft.validation_report or {})

    outcome = validation_l3.run_layer3_for_version(draft.id)
    assert outcome.ran is True
    assert outcome.passed is True
    assert outcome.est_eps_per_shard >= 1000

    draft.refresh_from_db()
    report = draft.validation_report
    assert report["status"] == "passed"
    assert report["dry_run"]["est_eps_per_shard"] >= 1000
    assert report["dry_run"]["traversals_completed"] >= 1


def test_run_layer3_does_not_mutate_the_immutable_manifest() -> None:
    draft = _create_builtin_draft()
    sha_before = draft.manifest_sha256
    manifest_before = draft.manifest
    validation_l3.run_layer3_for_version(draft.id)
    draft.refresh_from_db()
    assert draft.manifest_sha256 == sha_before
    assert draft.manifest == manifest_before


def test_run_layer3_skips_when_l1_l2_not_passed() -> None:
    draft = _create_builtin_draft()
    # Simulate a row whose L1+L2 has not passed (the §8.4 sequencing gate).
    draft.validation_report = {"status": "failed", "errors": [], "warnings": []}
    draft.save(update_fields=["validation_report"])
    outcome = validation_l3.run_layer3_for_version(draft.id)
    assert outcome.ran is False
    draft.refresh_from_db()
    assert "dry_run" not in draft.validation_report


def test_run_layer3_for_missing_version_raises() -> None:
    import uuid

    with pytest.raises(validation_l3.ManifestVersionMissing):
        validation_l3.run_layer3_for_version(uuid.uuid4())


def test_run_layer3_is_idempotent() -> None:
    draft = _create_builtin_draft()
    first = validation_l3.run_layer3_for_version(draft.id)
    second = validation_l3.run_layer3_for_version(draft.id)
    # Re-running on the immutable document re-derives the same content metrics.
    assert first.report["dry_run"]["mean_events_per_session"] == (
        second.report["dry_run"]["mean_events_per_session"]
    )
    assert first.report["dry_run"]["traversals_completed"] == (
        second.report["dry_run"]["traversals_completed"]
    )


# --- end-to-end: draft create -> validation task -> poll --------------------


def test_draft_create_runs_l3_task_and_poll_surfaces_dry_run(
    authed_workspace: AuthedWorkspace,
    django_capture_on_commit_callbacks: Any,
) -> None:
    # CELERY_TASK_ALWAYS_EAGER is on in tests, so the validation-queue job the
    # draft-create endpoint enqueues runs inline; the report poll (#30) then
    # surfaces the §8.3 dry_run block (the existing endpoint is the polling target).
    # The endpoint enqueues via transaction.on_commit (so a real worker never races
    # the request transaction's commit), so capture+execute the on-commit callbacks.
    manifest = valid_subset_manifest()
    manifest["metadata"]["slug"] = "l3draft"
    with django_capture_on_commit_callbacks(execute=True):
        resp = authed_workspace.client.post(
            "/api/v1/scenarios",
            data={"workspace_id": str(authed_workspace.workspace.id), "document": manifest},
            format="json",
        )
    assert resp.status_code == 201, resp.content

    poll = authed_workspace.client.get(
        "/api/v1/scenarios/l3draft/versions/1.0.0/validation"
        f"?workspace_id={authed_workspace.workspace.id}"
    )
    assert poll.status_code == 200
    body = poll.json()
    assert body["status"] == "passed"
    assert body["dry_run"] is not None
    assert body["dry_run"]["est_eps_per_shard"] >= 1000


# --- the GUARD command ------------------------------------------------------


def test_guard_passes_for_shipped_builtins() -> None:
    # No DB rows needed: the GUARD runs the engine directly on each builtin YAML.
    call_command("revalidate_builtins_l3", builtin_dir=str(_BUILTIN_DIR))


def test_guard_fails_on_a_sub_floor_builtin(tmp_path: Path) -> None:
    # A builtin whose machine livelocks (MAN-D602) must fail the GUARD.
    livelock_dir = tmp_path / "livelock"
    livelock_dir.mkdir()
    (livelock_dir / "1.0.0.yaml").write_text(_LIVELOCK_YAML, encoding="utf-8")
    with pytest.raises(CommandError) as exc:
        call_command("revalidate_builtins_l3", builtin_dir=str(tmp_path))
    assert "Layer-3 GUARD failed" in str(exc.value)
    assert "MAN-D602" in str(exc.value)


_LIVELOCK_YAML = """
manifest_schema: v0
metadata:
  slug: livelock
  version: 1.0.0
  title: Livelock
  actor_entity: users
entities:
  users:
    key_prefix: usr
    key_attribute: user_id
    attributes:
      tier: { generator: choice.uniform, params: { options: [free] } }
event_types:
  noop:
    payload:
      user_id: { from: actor.user_id }
state_machines:
  shopping_session:
    type: session
    binds: users
    initial: spinning
    states:
      spinning:
        remainder: stay
        transitions:
          - to: done
            probability: 0.95
            guard:
              all:
                - { path: actor.tier, op: eq, value: impossible }
          - to: spinning
            probability: 0.04
            emit: noop
      done:
        terminal: true
seeding:
  catalogs:
    users: { default: 100, min: 1, max: 1000 }
"""
