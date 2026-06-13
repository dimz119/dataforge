"""Layer 1b: WorkspaceScoped manager isolation + fail-closed context.

Asserts the scoped default manager filters to the active workspace, refuses to
run with no context (fail-closed, never unscoped), and that ``all_objects`` is
the deliberate escape hatch.
"""

from __future__ import annotations

import pytest

from tenancy.domain.context import (
    WorkspaceContextError,
    get_active_workspace_id,
    workspace_context,
)
from tenancy.domain.models import ApiKey, Membership

pytestmark = pytest.mark.django_db


def test_scoped_manager_raises_with_no_active_context() -> None:
    """A tenant query with no armed context fails closed, not unscoped."""
    assert get_active_workspace_id() is None
    with pytest.raises(WorkspaceContextError):
        list(Membership.objects.all())


def test_scoped_manager_filters_to_active_workspace(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """``objects`` returns only the active workspace's rows."""
    a = make_workspace("a-admin@example.com")
    b = make_workspace("b-admin@example.com")

    with workspace_context(a.workspace.id):
        rows = list(Membership.objects.all())
        assert {m.workspace_id for m in rows} == {a.workspace.id}

    with workspace_context(b.workspace.id):
        rows = list(Membership.objects.all())
        assert {m.workspace_id for m in rows} == {b.workspace.id}


def test_foreign_workspace_lookup_returns_nothing(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """A's membership is invisible while B's context is armed (isolation)."""
    a = make_workspace("a2-admin@example.com")
    b = make_workspace("b2-admin@example.com")
    a_membership = Membership.all_objects.filter(workspace=a.workspace).first()
    assert a_membership is not None

    with workspace_context(b.workspace.id):
        assert Membership.objects.filter(id=a_membership.id).first() is None


def test_all_objects_is_unscoped_escape_hatch(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """``all_objects`` sees every workspace's rows (the platform-internal hatch)."""
    a = make_workspace("a3-admin@example.com")
    b = make_workspace("b3-admin@example.com")
    workspace_ids = {m.workspace_id for m in ApiKey.all_objects.all()} | {
        m.workspace_id for m in Membership.all_objects.all()
    }
    assert a.workspace.id in {m.workspace_id for m in Membership.all_objects.all()}
    assert b.workspace.id in {m.workspace_id for m in Membership.all_objects.all()}
    # No assertion on api keys content; just that the unscoped manager runs.
    assert isinstance(workspace_ids, set)


def test_context_clears_on_exit(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """The context manager restores ``None`` after the block (no leak)."""
    a = make_workspace("a4-admin@example.com")
    with workspace_context(a.workspace.id):
        assert get_active_workspace_id() == a.workspace.id
    assert get_active_workspace_id() is None


def test_nested_context_restores_previous(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """Nested contexts restore the outer value, not unconditionally None."""
    a = make_workspace("a5-admin@example.com")
    b = make_workspace("b5-admin@example.com")
    with workspace_context(a.workspace.id):
        with workspace_context(b.workspace.id):
            assert get_active_workspace_id() == b.workspace.id
        assert get_active_workspace_id() == a.workspace.id
