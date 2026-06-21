"""Flow 2 explicit-registration rejections — the §6.4 worked corpus (Exit #4).

Phase-10 exit criterion #4: "REG-C001..C012 each rejected with the documented
problem code; compat violations surface as 409 with per-field details." Where
``tests/registry/test_compat.py`` exercises the pure §6 checker in isolation, this
suite drives the **Flow-2 control-plane path** end-to-end against a real published
``ecommerce.order_placed`` v1 — the path the ``manage.py registry register-version``
command and the seed step share (``registry.application.explicit_registration``).

Each candidate change against the registered latest carries a fixed verdict + code
(schema-registry §6.4 worked-rejection table): add-and-require → C003; an added
field with no binding → C007; drop a field → C001; retype nested ``quantity`` → C002;
widen the ``currency`` enum → C002; ``additionalProperties:true`` → C004; a ``_df_``
reserved name → C009; against a ``cdc.*`` subject → C012; an unknown subject → C011;
``--expected-latest`` mismatch → C008. Plus the §5.2 idempotent re-register no-op and
the ``--check`` dry-run (no write).

The Flow-2 write registers a global (NULL-workspace) ``schema_versions`` row, so this
suite — like every registry publish/registration test — runs under the maintenance
role (``DATABASE_URL=postgres://dataforge:dataforge``), not the app role (the §5.2
Class-H global write would otherwise violate the app-role RLS WITH CHECK).
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction

from registry.application import explicit_registration as flow2
from registry.application import services
from registry.domain.models import SchemaVersion, Subject

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"


def _latest_v1(subject_name: str = _SUBJECT) -> dict[str, Any]:
    """The published v1 document of ``subject_name`` (the gate baseline)."""
    op = Subject.objects.get(subject=subject_name)
    latest = SchemaVersion.objects.filter(subject=op).order_by("-version").first()
    assert latest is not None
    return copy.deepcopy(latest.json_schema)


def _register(
    subject_name: str,
    candidate: dict[str, Any],
    *,
    expected_latest: int | None = None,
    dry_run: bool = False,
) -> flow2.Flow2Outcome:
    """Run the Flow-2 path the way the command does (its own transaction)."""
    context = services.scenario_context_for_subject(subject_name)
    assert context is not None
    with transaction.atomic():
        return flow2.register_explicit_version(
            subject_name=subject_name,
            candidate=candidate,
            scenario_id=context.scenario_id,
            workspace_id=context.workspace_id,
            latest_manifest=context.latest_manifest,
            expected_latest=expected_latest,
            dry_run=dry_run,
        )


def _codes_of(subject_name: str, candidate: dict[str, Any], **kw: Any) -> list[str]:
    """Register and return the §6.3 report error codes (asserts it was rejected)."""
    with pytest.raises(flow2.Flow2Incompatible) as exc:
        _register(subject_name, candidate, **kw)
    return [e.code for e in exc.value.report.errors]


# --- the §6.4 worked-rejection table, one assertion per row -------------------


def test_reg_c003_add_and_require_is_rejected(published_ecommerce: Any) -> None:
    """C003: adding ``shipping_state`` to ``required`` violates the frozen required set."""
    candidate = _latest_v1()
    candidate["properties"]["shipping_state"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    candidate["required"].append("shipping_state")
    assert "REG-C003" in _codes_of(_SUBJECT, candidate)


def test_reg_c007_added_field_without_binding_is_rejected(published_ecommerce: Any) -> None:
    """C007: an added property with no resolvable ``x-df-binding`` is rejected."""
    candidate = _latest_v1()
    candidate["properties"]["shipping_state"] = {"type": "string"}  # no x-df-binding
    assert "REG-C007" in _codes_of(_SUBJECT, candidate)


def test_reg_c001_drop_field_is_rejected(published_ecommerce: Any) -> None:
    """C001: removing a registered field is non-additive."""
    candidate = _latest_v1()
    candidate["properties"].pop("shipping_country")
    candidate["required"] = [f for f in candidate["required"] if f != "shipping_country"]
    assert "REG-C001" in _codes_of(_SUBJECT, candidate)


def test_reg_c002_retype_nested_quantity_is_rejected(published_ecommerce: Any) -> None:
    """C002: retyping the nested ``items[].quantity`` fragment is a frozen-field change."""
    candidate = _latest_v1()
    candidate["properties"]["items"]["items"]["properties"]["quantity"] = {"type": "string"}
    assert "REG-C002" in _codes_of(_SUBJECT, candidate)


def test_reg_c002_widen_currency_enum_is_rejected(published_ecommerce: Any) -> None:
    """C002: widening the ``currency`` const/enum is a frozen-field change."""
    candidate = _latest_v1()
    candidate["properties"]["currency"] = {"enum": ["USD", "EUR"]}
    assert "REG-C002" in _codes_of(_SUBJECT, candidate)


def test_reg_c004_open_document_is_rejected(published_ecommerce: Any) -> None:
    """C004: ``additionalProperties:true`` opens the document (closed-doc rule)."""
    candidate = _latest_v1()
    candidate["additionalProperties"] = True
    assert "REG-C004" in _codes_of(_SUBJECT, candidate)


def test_reg_c009_reserved_df_name_is_rejected(published_ecommerce: Any) -> None:
    """C009: a ``_df_``-prefixed reserved property name is rejected."""
    candidate = _latest_v1()
    candidate["properties"]["_df_grade"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    assert "REG-C009" in _codes_of(_SUBJECT, candidate)


def test_reg_c012_cdc_subject_is_rejected(published_ecommerce: Any) -> None:
    """C012: a ``cdc.*`` subject cannot be explicitly evolved (manifest-only)."""
    candidate = _latest_v1("ecommerce.cdc.users")
    candidate["properties"]["extra"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    # cdc subjects have a scenario context too; the structural C012 fires regardless.
    codes = _codes_of("ecommerce.cdc.users", candidate)
    assert "REG-C012" in codes


def test_reg_c011_unknown_subject_is_rejected(published_ecommerce: Any) -> None:
    """C011: a subject no manifest has published is unknown (subjects are publish-made)."""
    out = io.StringIO()
    err = io.StringIO()
    with pytest.raises(CommandError) as exc:
        call_command(
            "registry",
            "register-version",
            "ecommerce.nonexistent_event",
            "--schema",
            _write_tmp_schema(_minimal_candidate()),
            stdout=out,
            stderr=err,
        )
    report = json.loads(str(exc.value))
    assert report["compatible"] is False
    assert [e["code"] for e in report["errors"]] == ["REG-C011"]


def test_reg_c008_expected_latest_mismatch_is_rejected(published_ecommerce: Any) -> None:
    """C008: ``--expected-latest`` not equal to the current latest aborts (optimistic CAS)."""
    candidate = _latest_v1()
    candidate["properties"]["shipping_state"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    # The subject is at v1; assert v5 → mismatch.
    assert "REG-C008" in _codes_of(_SUBJECT, candidate, expected_latest=5)


# --- the §5.2 accept paths: idempotent no-op + dry-run ------------------------


def test_idempotent_reregister_is_a_noop(published_ecommerce: Any) -> None:
    """Re-registering the *latest* fingerprint exits clean with no write (the seed re-run)."""
    before = SchemaVersion.objects.filter(subject__subject=_SUBJECT).count()
    outcome = _register(_SUBJECT, _latest_v1())
    assert outcome.idempotent is True
    assert outcome.registered is None
    after = SchemaVersion.objects.filter(subject__subject=_SUBJECT).count()
    assert after == before  # no version written


def test_check_dry_run_validates_without_writing(published_ecommerce: Any) -> None:
    """``--check``: a valid additive candidate passes §4 + §6 but writes nothing."""
    before = SchemaVersion.objects.filter(subject__subject=_SUBJECT).count()
    candidate = _latest_v1()
    candidate["properties"]["shipping_state"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    outcome = _register(_SUBJECT, candidate, dry_run=True)
    assert outcome.report.compatible is True
    assert outcome.registered is None  # dry-run wrote nothing
    after = SchemaVersion.objects.filter(subject__subject=_SUBJECT).count()
    assert after == before


def test_command_registers_then_is_idempotent(published_ecommerce: Any) -> None:
    """The CLI registers a valid v2, then a re-run of the same schema is a no-op."""
    path = _write_tmp_schema(_v2_candidate())
    out = io.StringIO()
    call_command("registry", "register-version", _SUBJECT, "--schema", path, stdout=out)
    assert SchemaVersion.objects.get(subject__subject=_SUBJECT, version=2)
    # Re-run: idempotent, exit 0, still exactly one v2.
    out2 = io.StringIO()
    call_command("registry", "register-version", _SUBJECT, "--schema", path, stdout=out2)
    assert "idempotent" in out2.getvalue().lower()
    assert SchemaVersion.objects.filter(subject__subject=_SUBJECT, version=2).count() == 1


# --- helpers ------------------------------------------------------------------


def _v2_candidate() -> dict[str, Any]:
    """A valid additive v2 (adds optional ``shipping_state`` with a resolvable binding)."""
    candidate = _latest_v1()
    candidate["properties"]["shipping_state"] = {
        "type": "string",
        "x-df-binding": {"from": "actor.address.state"},
    }
    return candidate


def _minimal_candidate() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {},
    }


def _write_tmp_schema(document: dict[str, Any]) -> str:
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(document, handle)
    handle.close()
    return handle.name
