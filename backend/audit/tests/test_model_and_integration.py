"""Model invariants, migration RLS coverage, and the cross-app emit seams."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.db.migrations.loader import MigrationLoader

from audit.application.writer import record_audit
from audit.domain.models import AuditLog
from audit.infra.rls import EnableAuditRowLevelSecurity
from audit.tests.conftest import UserFactory

pytestmark = pytest.mark.django_db


# --- M-6: RLS ships with the table -------------------------------------------
def test_audit_migration_carries_rls_operation() -> None:
    """The audit_log migration enables Class A RLS (M-6 / database-schema §9.5)."""
    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    ops = [
        op
        for migration in loader.disk_migrations.values()
        if migration.app_label == "audit"
        for op in migration.operations
        if isinstance(op, EnableAuditRowLevelSecurity)
    ]
    assert len(ops) == 1
    assert ops[0].table == "audit_log"
    assert ops[0].policy_class == "A"


def test_table_name_matches_schema() -> None:
    assert AuditLog._meta.db_table == "audit_log"


# --- INV-AUD-1: no update/delete surface (append-only by construction) --------
def test_no_writer_update_or_delete_symbols() -> None:
    """The writer module exposes only record_audit — no update/delete path."""
    from audit.application import writer

    public = {n for n in dir(writer) if not n.startswith("_")}
    # There is exactly one mutating entrypoint, and it only inserts.
    assert "record_audit" in public
    assert not {n for n in public if "update" in n.lower() or "delete" in n.lower()}


# --- the cross-app emit seams resolve to record_audit ------------------------
def test_identity_emit_seam_writes_account_level_entry(make_user: UserFactory) -> None:
    """identity.application.audit.emit -> record_audit (NULL workspace_id)."""
    from identity.application.audit import emit

    user = make_user()
    emit("identity.user.registered", actor=user, metadata={"email": user.email})
    entry = AuditLog.objects.get(action="identity.user.registered")
    assert entry.workspace_id is None  # account-level
    assert entry.actor_user_id == user.id


def test_tenancy_emit_seam_writes_workspace_entry(make_user: UserFactory) -> None:
    """tenancy.application.audit.emit -> record_audit (workspace-scoped)."""
    from tenancy.application.audit import emit

    user, ws = make_user(), uuid4()
    emit(
        "tenancy.api_key.created",
        actor=user,
        workspace_id=ws,
        target={"type": "api_key", "id": str(uuid4()), "label": "lab (3f8a…UxKz)"},
        metadata={"scopes": ["events:read"], "prefix": "3f8a"},
    )
    entry = AuditLog.objects.get(action="tenancy.api_key.created")
    assert entry.workspace_id == ws
    assert entry.metadata["prefix"] == "3f8a"


def test_tenancy_emit_secret_in_metadata_is_stripped(make_user: UserFactory) -> None:
    """Even via the seam, a stray secret never lands (INV-AUD-3 backstop)."""
    from tenancy.application.audit import emit

    user, ws = make_user(), uuid4()
    emit(
        "tenancy.api_key.created",
        actor=user,
        workspace_id=ws,
        metadata={"plaintext": "df_dev_3f8a_secret"},
    )
    entry = AuditLog.objects.get(action="tenancy.api_key.created")
    assert "df_dev_3f8a_secret" not in str(entry.metadata)


def test_record_audit_signature_matches_emit_call_kwargs(make_user: UserFactory) -> None:
    """record_audit accepts exactly the kwargs identity/tenancy pass."""
    record_audit(
        action="tenancy.workspace.created",
        actor=make_user(),
        workspace_id=uuid4(),
        target={"type": "workspace", "id": str(uuid4()), "label": "lab"},
        metadata={"slug": "lab", "plan": "free"},
    )
    assert AuditLog.objects.filter(action="tenancy.workspace.created").count() == 1
