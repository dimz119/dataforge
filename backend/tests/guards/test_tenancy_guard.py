"""GUARD meta-tests for ``check_tenancy`` (testing-strategy §7.4; exit crit #3).

Plants the three canary classes the Phase 2 exit criterion names and asserts the
static guard FAILS naming the offender, then that corrected controls PASS — the
no-false-positive-lock-in control. This is the static half of the "a breach
requires two simultaneous failures" proof: a planted unscoped model is red before
it can ship.

Canaries are ``managed = False`` models in a throwaway ``guard_canary`` app label
so they need no migration and never touch the real schema; they are passed to
``run_checks`` via ``extra_models``.
"""

from __future__ import annotations

import pytest
from django.db import models

from tenancy.api.viewset_base import ScopedModelViewSet
from tenancy.domain.scoping import WorkspaceScopedModel
from tenancy.infra import tenancy_check
from tenancy.infra.tenancy_check import run_checks

pytestmark = pytest.mark.guards

_APP = "guard_canary"


class _UnscopedNoWorkspaceId(models.Model):
    """Canary 1: tenant-shaped but no ``workspace_id`` and not exempt."""

    name = models.TextField()

    class Meta:
        app_label = _APP
        managed = False
        db_table = "canary_no_ws"


class _DefaultManagerCanary(WorkspaceScopedModel):
    """Canary 2: has ``workspace_id`` but overrides ``objects`` with a plain Manager."""

    workspace_id = models.UUIDField()
    objects = models.Manager()  # type: ignore[assignment]  # the offence: not scoped

    class Meta:
        app_label = _APP
        managed = False
        db_table = "canary_default_mgr"


class _CorrectCanary(WorkspaceScopedModel):
    """Control: a correctly-scoped tenant model (workspace_id + scoped manager)."""

    workspace_id = models.UUIDField()

    class Meta:
        app_label = _APP
        managed = False
        db_table = "memberships"  # reuse a table that HAS an RLS migration


def _violations_for(extra: list[type[models.Model]]) -> list[str]:
    return run_checks(extra_models=extra)


def test_canary_unclassified_model_fails_naming_it() -> None:
    violations = _violations_for([_UnscopedNoWorkspaceId])
    assert any(
        "guard_canary._UnscopedNoWorkspaceId" in v and "UNCLASSIFIED" in v for v in violations
    ), violations


def test_canary_default_manager_fails_naming_it() -> None:
    violations = _violations_for([_DefaultManagerCanary])
    joined = "\n".join(violations)
    assert "guard_canary._DefaultManagerCanary" in joined
    assert "UNSCOPED MANAGER" in joined


def test_corrected_canary_passes() -> None:
    """Control: a correctly-scoped model adds no violations (no false positive)."""
    violations = _violations_for([_CorrectCanary])
    # The corrected canary itself must not be flagged.
    assert not any("_CorrectCanary" in v for v in violations), violations


def test_unscoped_viewset_predicate() -> None:
    """Canary 3a: the guard's step-4 predicate ``is_scoped_viewset``."""
    from tenancy.api.viewset_base import is_scoped_viewset
    from tenancy.domain.models import Membership

    class _BadViewSet:  # does NOT extend ScopedModelViewSet
        queryset = Membership.all_objects.all()

    class _GoodViewSet(ScopedModelViewSet):
        queryset = Membership.all_objects.all()

    assert is_scoped_viewset(_BadViewSet) is False
    assert is_scoped_viewset(_GoodViewSet) is True


def test_unscoped_viewset_canary_fails_through_run_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canary 3b: a viewset over a tenant model, not extending the scoped base
    and not exempt, makes ``run_checks`` emit an UNSCOPED VIEWSET violation.

    Injected via the guard's viewset-discovery seam so the canary need not be
    wired into the live URLconf; this exercises step 4's full violation path.
    """
    from tenancy.domain.models import Membership

    class _BadViewSet:  # NOT in EXEMPT_VIEWSETS, does NOT extend the scoped base
        queryset = Membership.all_objects.all()

    path = "guard_canary.viewsets._BadViewSet"
    monkeypatch.setattr(
        tenancy_check, "_all_viewset_classes", lambda: {path: _BadViewSet}
    )
    violations = run_checks()
    joined = "\n".join(violations)
    assert "UNSCOPED VIEWSET" in joined
    assert path in joined


def test_corrected_viewset_canary_passes_through_run_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the corrected viewset (extends the scoped base) is not flagged."""
    from tenancy.domain.models import Membership

    class _GoodViewSet(ScopedModelViewSet):
        queryset = Membership.all_objects.all()

    monkeypatch.setattr(
        tenancy_check,
        "_all_viewset_classes",
        lambda: {"guard_canary.viewsets._GoodViewSet": _GoodViewSet},
    )
    violations = run_checks()
    assert not any("_GoodViewSet" in v for v in violations), violations


def test_real_schema_passes_the_guard() -> None:
    """The shipped schema passes (no planted canaries) — the control baseline."""
    assert run_checks() == []
