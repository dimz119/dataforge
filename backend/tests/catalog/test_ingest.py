"""Catalog ingest write path (plugin-arch §10.1, §12; api §4.6).

Draft creation: parse hardening + L1+L2, hook gating (MAN-V404 in workspace
manifests), slug collision (§4.1), version conflict (409), AI-4 draft quota.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from catalog.application import ingest
from catalog.domain.models import (
    STATUS_DRAFT,
    VISIBILITY_GLOBAL,
    VISIBILITY_WORKSPACE,
    ManifestVersion,
    Scenario,
)
from tests.catalog.fixtures import valid_subset_manifest

pytestmark = pytest.mark.django_db


def test_create_draft_global_owns_null_workspace() -> None:
    manifest = valid_subset_manifest()
    draft = ingest.create_draft(
        manifest, workspace_id=None, is_workspace_visibility=False
    )
    assert draft.status == STATUS_DRAFT
    assert draft.workspace_id is None
    assert draft.scenario.visibility == VISIBILITY_GLOBAL
    assert draft.validation_report["status"] == "passed"


def test_create_draft_workspace_visibility_owns_tenant() -> None:
    ws = uuid.uuid4()
    manifest = valid_subset_manifest()
    manifest["metadata"]["slug"] = "tenant_scn"
    draft = ingest.create_draft(manifest, workspace_id=ws, is_workspace_visibility=True)
    assert draft.workspace_id == ws
    assert draft.scenario.visibility == VISIBILITY_WORKSPACE
    assert draft.scenario.workspace_id == ws


def test_rejected_manifest_creates_no_draft() -> None:
    bad = valid_subset_manifest()
    # Trip a probability-sum violation in the session machine if present, else a
    # structural failure: drop the required metadata slug → MAN-S/V.
    del bad["metadata"]["slug"]
    with pytest.raises(ingest.ManifestRejected) as exc:
        ingest.create_draft(bad, workspace_id=None, is_workspace_visibility=False)
    assert exc.value.report["status"] == "failed"
    assert not ManifestVersion.objects.exists()


def test_version_conflict_on_duplicate() -> None:
    manifest = valid_subset_manifest()
    ingest.create_draft(manifest, workspace_id=None, is_workspace_visibility=False)
    with pytest.raises(ingest.VersionConflict):
        ingest.create_draft(
            copy.deepcopy(manifest), workspace_id=None, is_workspace_visibility=False
        )


def test_workspace_slug_may_not_shadow_global() -> None:
    manifest = valid_subset_manifest()
    ingest.create_draft(manifest, workspace_id=None, is_workspace_visibility=False)
    ws = uuid.uuid4()
    shadow = copy.deepcopy(manifest)
    with pytest.raises(ingest.SlugCollision):
        ingest.create_draft(shadow, workspace_id=ws, is_workspace_visibility=True)


def test_ai4_draft_quota_enforced() -> None:
    ws = uuid.uuid4()
    scenario = Scenario.objects.create(
        slug="quota_scn", title="x", visibility=VISIBILITY_WORKSPACE, workspace_id=ws
    )
    for i in range(ingest.MAX_DRAFT_VERSIONS_PER_WORKSPACE):
        ManifestVersion.objects.create(
            scenario=scenario,
            workspace_id=ws,
            version=f"1.0.{i}",
            manifest={"manifest_schema": "v0"},
            manifest_sha256=f"sha{i}",
            status=STATUS_DRAFT,
            validation_report={"status": "passed"},
        )
    with pytest.raises(ingest.DraftQuotaExceeded):
        ingest.enforce_draft_quota(ws)
    # Globals are exempt.
    ingest.enforce_draft_quota(None)


def test_resolve_scenario_workspace_first_then_global() -> None:
    ws = uuid.uuid4()
    Scenario.objects.create(slug="g", title="g", visibility=VISIBILITY_GLOBAL, workspace_id=None)
    ws_row = Scenario.objects.create(
        slug="w", title="w", visibility=VISIBILITY_WORKSPACE, workspace_id=ws
    )
    resolved_global = ingest.resolve_scenario("g", ws)
    resolved_ws = ingest.resolve_scenario("w", ws)
    assert resolved_global is not None and resolved_global.workspace_id is None
    assert resolved_ws is not None and resolved_ws.id == ws_row.id
    assert ingest.resolve_scenario("w", None) is None  # global lookup misses ws row


def test_canonicalize_sha_is_byte_stable() -> None:
    manifest = valid_subset_manifest()
    a = ingest.canonicalize(manifest)
    b = ingest.canonicalize(copy.deepcopy(manifest))
    assert a.sha256 == b.sha256
    assert a.slug == manifest["metadata"]["slug"]


def _hook_manifest() -> dict[str, Any]:
    """A manifest with a ``hook`` generator (banned in workspace manifests, MAN-V404)."""
    manifest = valid_subset_manifest()
    entities = manifest["entities"]
    first = next(iter(entities))
    attrs = entities[first]["attributes"]
    attrs["hooked"] = {"generator": "hook", "params": {"name": "anything"}}
    manifest["metadata"]["slug"] = "hooked_scn"
    return manifest


def test_hook_generator_rejected_in_workspace_manifest() -> None:
    with pytest.raises(ingest.ManifestRejected) as exc:
        ingest.create_draft(
            _hook_manifest(), workspace_id=uuid.uuid4(), is_workspace_visibility=True
        )
    codes = {e["code"] for e in exc.value.report["errors"]}
    assert "MAN-V404" in codes
