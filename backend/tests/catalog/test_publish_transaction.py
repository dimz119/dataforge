"""The manifest-publish transaction (plugin-arch §10.3; schema-registry §5.1 R-DER).

Exit criterion #2: publishing a manifest auto-derives v1 event schemas in the
same transaction; re-derivation is byte-identical. These run against the SQLite
unit DB (the RLS bite is exercised by the Postgres integration lane).
"""

from __future__ import annotations

from typing import Any

import pytest

from catalog.application import ingest, publish
from catalog.domain.models import STATUS_DRAFT, STATUS_PUBLISHED, ManifestVersion
from registry.domain.models import SchemaVersion, Subject
from registry.infra.canonical import fingerprint

pytestmark = pytest.mark.django_db


def test_publish_derives_and_registers_v1(published_ecommerce: Any) -> None:
    result = published_ecommerce
    assert result.manifest_version.status == STATUS_PUBLISHED
    assert result.manifest_version.published_at is not None
    # 9 business event subjects + 4 CDC subjects = 13 (the subset).
    assert len(result.registered) == 13
    assert all(r.created for r in result.registered)
    assert all(r.version == 1 for r in result.registered)

    subjects = {s.subject for s in Subject.objects.all()}
    assert "ecommerce.order_placed" in subjects
    assert "ecommerce.cdc.users" in subjects
    # Every subject has exactly one version, all v1, NULL workspace (global).
    versions = list(SchemaVersion.objects.all())
    assert len(versions) == 13
    assert all(v.version == 1 for v in versions)
    assert all(v.workspace_id is None for v in versions)
    assert all(v.compat_checked_against is None for v in versions)  # v1 has no prior


def test_derived_schemas_are_closed_and_all_required(published_ecommerce: Any) -> None:
    for sv in SchemaVersion.objects.all():
        doc = sv.json_schema
        assert doc["additionalProperties"] is False  # R-DER-3
        assert set(doc["required"]) == set(doc["properties"])  # all-required


def test_order_placed_schema_matches_golden(published_ecommerce: Any) -> None:
    sv = SchemaVersion.objects.get(subject__subject="ecommerce.order_placed")
    props = sv.json_schema["properties"]
    assert props["currency"] == {"const": "USD"}
    assert props["total"] == {"type": "string", "pattern": r"^-?\d+\.\d{1,4}$"}
    assert props["order_id"] == {"type": "string", "pattern": r"^ord_[0-9a-f]{16}$"}
    assert props["items"]["type"] == "array"
    assert props["items"]["items"]["type"] == "object"


def test_rederivation_is_byte_identical(published_ecommerce: Any) -> None:
    """Re-deriving the same manifest yields the same fingerprint (R-DER-4)."""
    from registry.infra.derive import derive_subjects

    definition = published_ecommerce.manifest_version
    rederived = {d.subject: fingerprint(d.document) for d in derive_subjects(definition.manifest)}
    stored = {sv.subject.subject: sv.fingerprint for sv in SchemaVersion.objects.all()}
    assert rederived == stored


def test_republishing_same_version_is_idempotent_noop(published_ecommerce: Any) -> None:
    """A second publish of an already-published version raises AlreadyPublished."""
    definition = published_ecommerce.manifest_version
    with pytest.raises(publish.AlreadyPublished):
        publish.publish_manifest_version(definition, actor="system", workspace_id=None)
    # No duplicate subjects/versions were created.
    assert SchemaVersion.objects.count() == 13


def test_publish_blocked_when_validation_not_passed(db: Any) -> None:
    """A draft whose report did not pass cannot publish (INV-CAT-2)."""
    from catalog.domain.models import Scenario

    scenario = Scenario.objects.create(
        slug="failing", title="x", visibility="global", workspace_id=None
    )
    draft = ManifestVersion.objects.create(
        scenario=scenario,
        workspace_id=None,
        version="1.0.0",
        manifest={"manifest_schema": "v0"},
        manifest_sha256="deadbeef",
        status=STATUS_DRAFT,
        validation_report={"status": "failed", "errors": [{"code": "MAN-V201"}]},
    )
    with pytest.raises(publish.PublishNotReady):
        publish.publish_manifest_version(draft, actor="system", workspace_id=None)
    assert not Subject.objects.exists()


def test_publish_transaction_atomic_on_registration_failure(
    db: Any, builtin_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If registration raises, the version stays draft and no subjects persist (R-DER)."""
    draft = ingest.create_draft(
        builtin_text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("registration failure")

    monkeypatch.setattr(publish, "register_derived_schemas", _boom)
    with pytest.raises(RuntimeError):
        publish.publish_manifest_version(draft, actor="system", workspace_id=None)
    draft.refresh_from_db()
    assert draft.status == STATUS_DRAFT  # rolled back
    assert not Subject.objects.exists()
    assert not SchemaVersion.objects.exists()
