"""Layer 3: the mandatory scoped viewset base (security §4.3).

Every DRF viewset over a tenant model MUST extend ``ScopedModelViewSet`` (the
``check_tenancy`` guard fails the build otherwise, step 4). It composes the
Layer 3 permission stack and overrides ``get_queryset`` to use the scoped default
manager (``Model.objects`` — already workspace-filtered by the contextvar), so a
foreign-workspace object id resolves to *no row* → 404 (W-3 masking), never 403.

This base is the seam future phases (catalog, registry, streams, …) build their
tenant CRUD on. Phase 2 ships it plus the alias ``WorkspaceScopedViewSet`` (the
name security-architecture §4.3 uses), and a default ``required_scopes = ()``
that data-plane viewsets override per endpoint.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import Model, QuerySet
from rest_framework import viewsets

from tenancy.application.permissions import (
    HasKeyScope,
    HasObjectWorkspace,
    IsAuthenticatedPrincipal,
)


class ScopedModelViewSet(viewsets.ModelViewSet):  # type: ignore[type-arg]
    """Base for every tenant-model viewset (security §4.3, step 4).

    Subclasses set ``queryset`` (or ``model``) and ``serializer_class``; the base
    enforces the scoped queryset + the Layer 3 permission stack. Data-plane
    viewsets set ``required_scopes`` per the §3.2.2 endpoint mapping.
    """

    # Default required scope set (empty = no key-scope gate); per-view overridable.
    required_scopes: ClassVar[tuple[str, ...]] = ()

    permission_classes = [IsAuthenticatedPrincipal, HasKeyScope, HasObjectWorkspace]

    def get_queryset(self) -> QuerySet[Model]:
        # The *scoped* default manager — already workspace-filtered by the active
        # contextvar (Layer 1b). A foreign id returns no row → 404 (W-3).
        qs: QuerySet[Model] = super().get_queryset()
        return qs


# The name security-architecture §4.3 uses for the same base.
WorkspaceScopedViewSet = ScopedModelViewSet


def is_scoped_viewset(view_cls: Any) -> bool:
    """True iff ``view_cls`` is (a subclass of) the scoped viewset base.

    Used by the ``check_tenancy`` guard (step 4) to classify viewsets.
    """
    return isinstance(view_cls, type) and issubclass(view_cls, ScopedModelViewSet)
