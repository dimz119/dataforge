"""Layer 1b: the mandatory ``WorkspaceScoped`` manager + model base.

Every tenant-owned model subclasses ``WorkspaceScopedModel`` (a non-null,
indexed ``workspace_id`` plus a declarative tenant marker) and uses
``WorkspaceScopedManager`` as its default ``objects`` manager. The manager's
``get_queryset`` reads the request-bound workspace context (``domain.context``)
and filters ``workspace_id = <active>``; with no active context it **raises**
(fail-closed) rather than returning unscoped rows (security-architecture §4.1).

``all_objects`` is the unscoped escape hatch for platform internals only; every
use site must carry a ``# tenancy: unscoped — <reason>`` marker that the
``check_tenancy`` guard accounts for (security §4.1, step 5).

The declarative marker ``WorkspaceScopedModel.is_tenant_owned`` is the *closed
classification* the ``check_tenancy`` guard drives from — never a hardcoded list
(brief: "drive from a declarative marker").
"""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

from django.db import models

from tenancy.domain.context import require_active_workspace_id

_M = TypeVar("_M", bound=models.Model)


class WorkspaceScopedQuerySet(models.QuerySet[_M]):
    """A queryset that knows how to (re-)scope to the active workspace."""

    def for_active_workspace(self) -> WorkspaceScopedQuerySet[_M]:
        """Filter to the active workspace; raise if no context is armed."""
        return self.filter(workspace_id=require_active_workspace_id())


class WorkspaceScopedManager(models.Manager[_M]):
    """Default manager that filters every query by the active workspace context.

    ``get_queryset`` is the chokepoint: ``Model.objects.all()``,
    ``.filter(...)``, ``.get(...)`` — all run ``WHERE workspace_id = <active>``.
    With no active context it raises ``WorkspaceContextError`` (fail-closed), so
    a tenant query can never accidentally span workspaces.
    """

    # The scoped default manager must not be used in migrations (it is
    # fail-closed); platform internals use all_objects instead.
    use_in_migrations = False

    def get_queryset(self) -> WorkspaceScopedQuerySet[_M]:
        workspace_id = require_active_workspace_id()
        return WorkspaceScopedQuerySet(self.model, using=self._db).filter(
            workspace_id=workspace_id
        )

    def all_workspaces(self) -> WorkspaceScopedQuerySet[_M]:
        """Unscoped queryset — internal escape hatch (see ``all_objects``).

        ``# tenancy: unscoped — manager escape hatch, guarded by check_tenancy``
        """
        return WorkspaceScopedQuerySet(self.model, using=self._db)


class AllObjectsManager(models.Manager[_M]):
    """Unscoped manager exposed as ``Model.all_objects`` (platform internals).

    Returns every workspace's rows. Use is allow-listed and counted by the
    ``check_tenancy`` guard; every call site carries a ``# tenancy: unscoped``
    marker (security §4.1).
    """

    use_in_migrations = True

    def get_queryset(self) -> WorkspaceScopedQuerySet[_M]:
        return WorkspaceScopedQuerySet(self.model, using=self._db)


class WorkspaceScopedModel(models.Model):
    """Abstract base for every tenant-owned model (INV-TEN-1).

    Provides the scoped default manager, the unscoped ``all_objects`` escape
    hatch, and the declarative tenant marker the ``check_tenancy`` guard reads.
    Each concrete subclass declares its own ``workspace`` FK with
    ``db_column="workspace_id"`` (the denormalized non-null tenant column, C-8) —
    the base does not declare the column itself so models whose tenant id is the
    PK (``workspace_quotas``) can promote it without a field clash. The guard
    asserts the presence of a ``workspace_id`` field on every subclass.

    Concrete subclasses still set their own ``Meta.db_table`` (C-2), indexes,
    and constraints.
    """

    # Declarative classification marker — the closed registry the CI guard drives
    # from (security §4.1 step 1). Subclasses inherit ``True``; non-tenant models
    # never subclass this, so they are forced into ``tenancy_exempt`` instead.
    is_tenant_owned: ClassVar[bool] = True

    objects: ClassVar[WorkspaceScopedManager[Any]] = WorkspaceScopedManager()
    all_objects: ClassVar[AllObjectsManager[Any]] = AllObjectsManager()

    class Meta:
        abstract = True
        # Django uses ``_base_manager`` for FK-descriptor / related-object lookups
        # and ``refresh_from_db``; pointing it at the *unscoped* manager keeps the
        # ORM's internals working (they must not be fail-closed), while the
        # *default* manager (``objects`` — first declared) stays scoped for all
        # application queries. Belt-and-suspenders: every related fetch is still
        # RLS-filtered at the DB (Layer 2).
        base_manager_name = "all_objects"
