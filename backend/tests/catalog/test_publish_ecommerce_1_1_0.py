"""Phase 8 P8-01: the FULL 8-entity ecommerce 1.1.0 builtin registers (catalog).

The full manifest ships as DATA at ``backend/catalog/builtin/ecommerce/1.1.0.yaml``
(copied verbatim from the normative §2 manifest of
``specs/04-engines/scenarios/ecommerce.md``, paper-validated at Phase 0). This test
asserts it ingests + publishes through the existing catalog pipeline (L1+L2) and
that the publish transaction derives v1 for all 29 subjects — the 21 business event
types and the 8 ``ecommerce.cdc.{entity}`` CDC subjects (R-DER-1, schema-registry
§5.1). 1.0.0 stays published alongside it (INV-CAT-1/5).

L3 / engine-execution capability for the generalized guards, curves, and CDC
emission lands with later Phase-8 agents; registration here is L1+L2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from catalog.application import ingest, publish
from catalog.domain.models import STATUS_PUBLISHED, ManifestVersion, Scenario
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_BUILTIN_1_1_0 = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.1.0.yaml"
)

# The 8 CDC subjects every full-manifest publish must derive (one per entity,
# R-DER-1). Asserted explicitly so a dropped or renamed entity is caught.
EXPECTED_CDC_SUBJECTS = {
    "ecommerce.cdc.users",
    "ecommerce.cdc.products",
    "ecommerce.cdc.orders",
    "ecommerce.cdc.payments",
    "ecommerce.cdc.refunds",
    "ecommerce.cdc.inventory",
    "ecommerce.cdc.shipments",
    "ecommerce.cdc.reviews",
}

# 21 business event types + 8 CDC subjects = 29 derived v1 subjects.
EXPECTED_SUBJECT_COUNT = 29


@pytest.fixture
def published_ecommerce_full() -> Any:
    """Ingest + publish the full ecommerce 1.1.0 builtin (global, NULL workspace).

    Mirrors ``sync_builtin_scenarios``: a draft through L1+L2 then the publish
    transaction (derive + register v1 for every subject, R-DER).
    """
    text = _BUILTIN_1_1_0.read_text(encoding="utf-8")
    draft = ingest.create_draft(
        text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )
    return publish.publish_manifest_version(draft, actor="system", workspace_id=None)


def test_full_manifest_registers_as_global_published(published_ecommerce_full: Any) -> None:
    result = published_ecommerce_full
    version = result.manifest_version
    assert version.status == STATUS_PUBLISHED
    assert version.published_at is not None
    assert version.builtin is True
    scenario = Scenario.objects.get(slug="ecommerce")
    assert scenario.workspace_id is None  # global
    assert scenario.visibility == "global"
    full = ManifestVersion.objects.get(scenario=scenario, version="1.1.0")
    assert full.workspace_id is None


def test_full_manifest_derives_all_29_subjects(published_ecommerce_full: Any) -> None:
    result = published_ecommerce_full
    assert len(result.registered) == EXPECTED_SUBJECT_COUNT
    assert all(r.created for r in result.registered)
    assert all(r.version == 1 for r in result.registered)

    subjects = {s.subject for s in Subject.objects.all()}
    assert "ecommerce.order_placed" in subjects
    assert "ecommerce.refund_approved" in subjects
    assert "ecommerce.account_closed" in subjects
    assert Subject.objects.count() == EXPECTED_SUBJECT_COUNT
    assert SchemaVersion.objects.count() == EXPECTED_SUBJECT_COUNT
    assert all(v.version == 1 for v in SchemaVersion.objects.all())
    assert all(v.workspace_id is None for v in SchemaVersion.objects.all())


def test_full_manifest_derives_all_cdc_subjects(published_ecommerce_full: Any) -> None:
    subjects = {s.subject for s in Subject.objects.all()}
    assert EXPECTED_CDC_SUBJECTS <= subjects
    # Each cdc.{entity} subject has a closed-profile v1 row-image schema (R-DER-3).
    for cdc_subject in EXPECTED_CDC_SUBJECTS:
        sv = SchemaVersion.objects.get(subject__subject=cdc_subject)
        assert sv.version == 1
        assert sv.json_schema["additionalProperties"] is False
        assert set(sv.json_schema["required"]) == set(sv.json_schema["properties"])


def test_full_manifest_coexists_with_subset_1_0_0() -> None:
    """1.1.0 publishes alongside 1.0.0; both stay published (INV-CAT-1/5).

    The co-deploy publishes 1.0.0 first (cdc.users v1, every field required —
    R-DER-3), then 1.1.0 re-derives the shared ``ecommerce.cdc.users`` subject with
    the new ``status`` attribute. Per §1.1/§4.1 REQ-RULE this registers cdc.users
    **VERSION 2** with ``status`` OPTIONAL (``required`` carried forward from v1
    exactly), passing the BACKWARD_ADDITIVE gate as an additive minor bump.
    """
    from django.core.management import call_command

    builtin_dir = _BUILTIN_1_1_0.parents[1]
    call_command("sync_builtin_scenarios", builtin_dir=str(builtin_dir))
    scenario = Scenario.objects.get(slug="ecommerce")
    v100 = ManifestVersion.objects.get(scenario=scenario, version="1.0.0")
    v110 = ManifestVersion.objects.get(scenario=scenario, version="1.1.0")
    assert v100.status == STATUS_PUBLISHED
    assert v110.status == STATUS_PUBLISHED
    cdc = {s.subject for s in Subject.objects.all() if ".cdc." in s.subject}
    assert EXPECTED_CDC_SUBJECTS <= cdc

    # cdc.users now has TWO versions: v1 (5 attrs + key + 2 timestamps, all
    # required) from 1.0.0, v2 adding optional ``status`` from 1.1.0 (§1.1/§4.1).
    users = Subject.objects.get(subject="ecommerce.cdc.users")
    versions = list(SchemaVersion.objects.filter(subject=users).order_by("version"))
    assert [v.version for v in versions] == [1, 2]
    v1, v2 = versions
    assert "status" not in v1.json_schema["properties"]
    assert set(v1.json_schema["required"]) == set(v1.json_schema["properties"])
    # REQ-RULE: v2 adds ``status`` to properties only; required carried forward.
    assert "status" in v2.json_schema["properties"]
    assert "status" not in v2.json_schema["required"]
    assert set(v2.json_schema["required"]) == set(v1.json_schema["required"])
    assert v2.json_schema["properties"]["status"]["x-df-binding"] is not None

    # Every OTHER CDC subject (unchanged between 1.0.0's subset and 1.1.0) stays at
    # version 1 — only the subjects whose row image actually changed get a v2.
    for cdc_subject in EXPECTED_CDC_SUBJECTS - {"ecommerce.cdc.users"}:
        sv = list(SchemaVersion.objects.filter(subject__subject=cdc_subject))
        # New entities (1.1.0-only) register v1; subjects shared with 1.0.0 whose
        # row image is identical stay at v1 (R-DER-4 no-op). None re-version here.
        assert [s.version for s in sv] == [1], cdc_subject
