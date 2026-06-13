"""Tests for the audit writer (INV-AUD-1..3): the single INSERT site."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import structlog
from django.db import transaction

from audit.application.writer import record_audit
from audit.domain.models import ACTOR_API_KEY, ACTOR_SYSTEM, ACTOR_USER, AuditLog
from audit.tests.conftest import UserFactory

pytestmark = pytest.mark.django_db


# --- INV-AUD-2: same-transaction write (rollback drops the audit row) ---------
def test_audit_row_rolled_back_with_caller_transaction(make_user: UserFactory) -> None:
    """An audit write joins the caller's atomic block — rollback drops it too."""
    user = make_user()

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with transaction.atomic():  # the "mutation" transaction (savepoint here)
            record_audit(
                action="tenancy.workspace.created",
                actor=user,
                workspace_id=uuid4(),
                target={"type": "workspace", "id": str(uuid4()), "label": "lab"},
                metadata={"slug": "lab"},
            )
            # A row exists inside the open transaction...
            assert AuditLog.objects.filter(action="tenancy.workspace.created").exists()
            raise _Boom  # ...the mutation then fails, rolling the whole txn back.

    # ...so the audit row is gone (INV-AUD-2: same commit/rollback fate).
    assert not AuditLog.objects.filter(action="tenancy.workspace.created").exists()


def test_audit_row_commits_with_caller_transaction(make_user: UserFactory) -> None:
    """The happy path: the audit row commits with the mutation."""
    user = make_user()
    with transaction.atomic():
        entry = record_audit(action="identity.user.logged_in", actor=user)
    persisted = AuditLog.objects.get(audit_id=entry.audit_id)
    assert persisted.action == "identity.user.logged_in"
    assert persisted.actor_type == ACTOR_USER
    assert persisted.actor_user_id == user.id


# --- INV-AUD-3: entries never contain secrets ---------------------------------
def test_secret_shaped_metadata_is_stripped(make_user: UserFactory) -> None:
    """No key material / password / token value is ever persisted (INV-AUD-3)."""
    user = make_user()
    entry = record_audit(
        action="tenancy.api_key.created",
        actor=user,
        workspace_id=uuid4(),
        target={"type": "api_key", "id": str(uuid4()), "label": "lab-key (3f8a…UxKz)"},
        metadata={
            "prefix": "3f8a",  # allowed reference
            "scopes": ["events:read"],  # allowed reference
            "plaintext": "df_dev_3f8a_supersecretvalue",  # MUST be stripped
            "key_hash": "deadbeef" * 8,  # MUST be stripped
            "password": "hunter2",  # MUST be stripped
            "refresh_token": "ey...zz",  # MUST be stripped (contains 'token')
            "nested": {"api_key": "df_dev_xxx", "label": "ok"},  # nested strip
        },
    )
    persisted = AuditLog.objects.get(audit_id=entry.audit_id)
    blob = str(persisted.metadata)
    # No secret values survive anywhere in the stored metadata.
    for secret in ("supersecretvalue", "deadbeef", "hunter2", "ey...zz", "df_dev_xxx"):
        assert secret not in blob, f"secret leaked into audit metadata: {secret}"
    # Non-secret references are retained.
    assert persisted.metadata["prefix"] == "3f8a"
    assert persisted.metadata["scopes"] == ["events:read"]
    # The non-secret label is retained too (it carries no secret material).
    assert persisted.metadata["nested"]["label"] == "ok"
    # The redacted markers are present (shape preserved, value gone).
    assert persisted.metadata["plaintext"] == "[redacted]"
    assert persisted.metadata["nested"]["api_key"] == "[redacted]"


# --- request_id stamping ------------------------------------------------------
def test_request_id_stamped_from_contextvar(make_user: UserFactory) -> None:
    """The bound request correlation id is stamped onto the entry (§7.1)."""
    user = make_user()
    rid = str(uuid4())
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        entry = record_audit(action="identity.user.registered", actor=user)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")
    assert AuditLog.objects.get(audit_id=entry.audit_id).request_id == rid


def test_request_id_null_outside_request(make_user: UserFactory) -> None:
    """Outside a request (system/Celery/shell) there is no correlation id."""
    structlog.contextvars.clear_contextvars()
    entry = record_audit(action="identity.user.registered", actor=make_user())
    assert AuditLog.objects.get(audit_id=entry.audit_id).request_id is None


# --- actor resolution (the §7.1 actor-presence CHECK) -------------------------
def test_none_actor_records_system() -> None:
    entry = record_audit(action="tenancy.api_key.expired", actor=None)
    persisted = AuditLog.objects.get(audit_id=entry.audit_id)
    assert persisted.actor_type == ACTOR_SYSTEM
    assert persisted.actor_user_id is None
    assert persisted.actor_api_key_id is None


def test_system_sentinel_actor_records_system() -> None:
    entry = record_audit(action="streams.stream.pause_requested", actor="system")
    assert AuditLog.objects.get(audit_id=entry.audit_id).actor_type == ACTOR_SYSTEM


def test_api_key_principal_actor_records_api_key() -> None:
    class _Principal:
        api_key_id = uuid4()

    principal = _Principal()
    entry = record_audit(action="delivery.batch.downloaded", actor=principal)
    persisted = AuditLog.objects.get(audit_id=entry.audit_id)
    assert persisted.actor_type == ACTOR_API_KEY
    assert persisted.actor_api_key_id == principal.api_key_id
    assert persisted.actor_user_id is None


def test_workspace_id_coerced_to_uuid(make_user: UserFactory) -> None:
    """A string workspace_id (as may arrive from a view) is stored as a UUID."""
    ws = uuid4()
    entry = record_audit(
        action="tenancy.workspace.updated", actor=make_user(), workspace_id=str(ws)
    )
    stored = AuditLog.objects.get(audit_id=entry.audit_id).workspace_id
    assert isinstance(stored, UUID) and stored == ws


def test_none_target_yields_empty_non_null_refs(make_user: UserFactory) -> None:
    """An action with no object (e.g. login) still satisfies NOT NULL columns."""
    entry = record_audit(action="identity.user.logged_in", actor=make_user())
    persisted = AuditLog.objects.get(audit_id=entry.audit_id)
    assert persisted.target_type == ""
    assert persisted.target_id == ""
