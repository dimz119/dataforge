"""CON §8.1 — publish-transaction integration (exit criterion #2).

Three binding claims, pinned end-to-end against real DB rows:

1. **One transaction, all subjects.** Publishing ``ecommerce 1.0.0`` registers
   version 1 for *every* derived subject (9 business + 4 CDC = 13) atomically — and
   a re-publish whose derivation breaks ``BACKWARD_ADDITIVE`` for even one subject
   rolls the **whole** thing back (no partial versions, the manifest stays
   unpublished). This is the R-DER "either both commit or neither" guarantee.
2. **Re-derivation is byte-identical.** The schema stored at publish equals a fresh
   derivation of the same manifest, byte-for-byte (comparison form).
3. **Compat rejects a field removal, naming it.** The ``BACKWARD_ADDITIVE`` checker
   rejects a candidate that drops a registered field and the error message names the
   removed field (the §6.3 ``{code, path, message}`` shape; MAN-V501 at publish).

DB-backed (``django_db``); structured to run under Postgres in CI.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from catalog.application import ingest, publish
from registry.domain.models import SchemaVersion, Subject
from registry.infra.canonical import canonical_bytes, comparison_form
from registry.infra.compat import check_backward_additive
from registry.infra.derive import derive_subjects

pytestmark = pytest.mark.django_db

_BUILTIN = (
    Path(__file__).resolve().parents[2] / "catalog" / "builtin" / "ecommerce" / "1.0.0.yaml"
)

_EXPECTED_SUBJECT_COUNT = 13  # 9 business event types + 4 CDC subjects (subset)


def _manifest() -> dict[str, Any]:
    return ingest.canonicalize(_BUILTIN.read_text(encoding="utf-8")).document


# --- Claim 1a: one transaction registers v1 for every derived subject ---------


def test_publish_registers_v1_for_every_derived_subject(published_ecommerce: Any) -> None:
    subjects = list(Subject.objects.all())
    assert len(subjects) == _EXPECTED_SUBJECT_COUNT
    # Exactly one version per subject, and it is version 1.
    for subject in subjects:
        versions = list(SchemaVersion.objects.filter(subject=subject).order_by("version"))
        assert [v.version for v in versions] == [1], subject.subject
    # The published manifest version drove exactly these registrations.
    registered = {r.subject for r in published_ecommerce.registered if r.created}
    assert registered == {s.subject for s in subjects}


# --- Claim 1b: a non-additive re-publish rolls the whole transaction back ------


def test_non_additive_republish_rolls_back_atomically(published_ecommerce: Any) -> None:
    """A 1.1.0 that retypes one existing payload field must abort the whole publish."""
    before_versions = SchemaVersion.objects.count()
    before_subjects = Subject.objects.count()

    evolved = copy.deepcopy(_manifest())
    evolved["metadata"]["version"] = "1.1.0"
    # Retype order_placed.total from a decimal-string fragment to a boolean — a
    # frozen-field change → REG-C002 on the ecommerce.order_placed subject.
    evolved["event_types"]["order_placed"]["payload"]["total"] = {
        "generated": {"generator": "choice.boolean"}
    }

    draft = ingest.create_draft(
        evolved, workspace_id=None, is_workspace_visibility=False, builtin=True
    )
    with pytest.raises(publish.ManifestSchemaCompatError) as exc:
        publish.publish_manifest_version(draft, actor="system", workspace_id=None)

    assert exc.value.subject == "ecommerce.order_placed"
    assert any(e["code"] == "MAN-V501" for e in exc.value.errors)
    # Nothing committed: no new versions, no new subjects, the draft stayed a draft.
    assert SchemaVersion.objects.count() == before_versions
    assert Subject.objects.count() == before_subjects
    draft.refresh_from_db()
    assert draft.status == "draft"
    assert draft.published_at is None


# --- Claim 2: stored schema == fresh derivation, byte-identical ---------------


def test_stored_schema_is_byte_identical_to_fresh_derivation(published_ecommerce: Any) -> None:
    fresh = {
        d.subject: canonical_bytes(comparison_form(d.document))
        for d in derive_subjects(_manifest())
    }
    stored = {
        v.subject.subject: canonical_bytes(comparison_form(v.json_schema))
        for v in SchemaVersion.objects.select_related("subject")
    }
    assert stored == fresh


# --- Claim 3: BACKWARD_ADDITIVE rejects a field removal, naming the field ------


def test_backward_additive_rejects_field_removal_naming_it(published_ecommerce: Any) -> None:
    op = Subject.objects.get(subject="ecommerce.order_placed")
    latest = SchemaVersion.objects.filter(subject=op).order_by("-version").first()
    assert latest is not None
    registered = latest.json_schema

    # Drop the 'total' field from a candidate built off the registered schema.
    candidate = copy.deepcopy(registered)
    candidate["properties"].pop("total")
    candidate["required"] = [f for f in candidate.get("required", []) if f != "total"]

    errors = check_backward_additive(registered, candidate)
    removal = [e for e in errors if e.code == "REG-C001"]
    assert removal, [e.to_dict() for e in errors]
    assert any("total" in e.message for e in removal)
    assert removal[0].path == "/properties/total"
