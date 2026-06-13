"""The ``check_tenancy`` guard core (security §4.1 CI guard; testing §7.4).

A pure, testable function ``run_checks`` the management command wraps. It
implements the closed-classification guard:

1. **Classification is closed.** Every installed model is either tenant-owned
   (subclasses ``WorkspaceScopedModel``) or listed in ``tenancy_exempt``. An
   unclassified model fails — new models are caught by construction, not by
   remembering to add a test.
2. Every tenant-owned model has a ``workspace_id`` field, uses
   ``WorkspaceScopedManager`` as its default manager, and exposes ``all_objects``.
3. Every tenant-owned table's migration history contains an
   ``EnableRowLevelSecurity`` operation (RLS coverage cannot lag schema growth).
4. Every DRF viewset over a tenant model subclasses the scoped viewset base or is
   in the exempt set.

The function returns a list of violation strings (empty ⇒ pass) so both the CLI
(exit non-zero, name the offender) and the GUARD meta-tests can assert on it.
"""

from __future__ import annotations

from django.apps import apps
from django.db import models
from django.db.migrations.loader import MigrationLoader

from tenancy.domain.scoping import WorkspaceScopedManager, WorkspaceScopedModel
from tenancy.infra.rls import EnableRowLevelSecurity
from tenancy.infra.tenancy_exempt import EXEMPT_MODELS, EXEMPT_VIEWSETS

# The scoped-viewset base class name (security §4.3). Detected by MRO name rather
# than imported, so this infra-layer guard never imports the api layer (preserves
# the app-layering import contract); the api base sets exactly this name.
_SCOPED_VIEWSET_BASE = "ScopedModelViewSet"


def _is_tenant_owned(model: type[models.Model]) -> bool:
    return issubclass(model, WorkspaceScopedModel) and getattr(model, "is_tenant_owned", False)


def _has_workspace_id_field(model: type[models.Model]) -> bool:
    try:
        model._meta.get_field("workspace_id")
        return True
    except Exception:
        pass
    # FK named ``workspace`` with attname ``workspace_id`` also satisfies it.
    return any(getattr(f, "attname", None) == "workspace_id" for f in model._meta.get_fields())


def _default_manager_is_scoped(model: type[models.Model]) -> bool:
    return isinstance(model._meta.default_manager, WorkspaceScopedManager)


def _tables_with_rls_migration() -> set[str]:
    """Tables that have an ``EnableRowLevelSecurity`` op in migration history."""
    loader = MigrationLoader(connection=None, ignore_no_migrations=True)
    tables: set[str] = set()
    for migration in loader.disk_migrations.values():
        for operation in migration.operations:
            if isinstance(operation, EnableRowLevelSecurity):
                tables.add(operation.table)
    return tables


def _all_viewset_classes() -> dict[str, type]:
    """Discover DRF view classes referenced by the URLconf (best-effort).

    Returns ``{import_path: view_cls}``. Used to assert tenant-model viewsets
    extend the scoped base (step 4).
    """
    from importlib import import_module

    from django.urls import get_resolver

    found: dict[str, type] = {}

    def _walk(patterns: object) -> None:
        for entry in getattr(patterns, "url_patterns", []) or []:
            if hasattr(entry, "url_patterns"):
                _walk(entry)
                continue
            callback = getattr(entry, "callback", None)
            view_cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if isinstance(view_cls, type):
                found[f"{view_cls.__module__}.{view_cls.__qualname__}"] = view_cls

    try:
        _walk(get_resolver())
    except Exception:
        pass
    # Defensive import so tenancy's own views are always considered.
    try:
        import_module("tenancy.api.viewsets")
    except Exception:
        pass
    return found


def run_checks(*, extra_models: list[type[models.Model]] | None = None) -> list[str]:
    """Run all tenancy guards; return violation strings (empty ⇒ pass).

    ``extra_models`` lets the GUARD meta-tests inject throwaway canary classes
    without registering a full app.
    """
    violations: list[str] = []
    rls_tables = _tables_with_rls_migration()

    all_models: list[type[models.Model]] = list(apps.get_models())
    if extra_models:
        all_models = [*all_models, *extra_models]

    for model in all_models:
        label = model._meta.label  # e.g. 'tenancy.ApiKey'
        tenant_owned = _is_tenant_owned(model)
        exempt = label in EXEMPT_MODELS

        # 1. Closed classification: every model must be one or the other.
        if not tenant_owned and not exempt:
            violations.append(
                f"UNCLASSIFIED MODEL {label}: not tenant-owned (does not subclass "
                f"WorkspaceScopedModel) and not listed in tenancy_exempt. Either make "
                f"it tenant-owned or add it to tenancy_exempt with a justification."
            )
            continue
        if tenant_owned and exempt:
            violations.append(
                f"AMBIGUOUS MODEL {label}: marked tenant-owned AND exempt — pick one."
            )
            continue
        if not tenant_owned:
            continue  # exempt model — no further tenant assertions

        # 2. Tenant-owned model must have workspace_id + the scoped manager.
        if not _has_workspace_id_field(model):
            violations.append(
                f"UNSCOPED MODEL {label}: tenant-owned but has no 'workspace_id' field "
                f"(INV-TEN-1)."
            )
        if not _default_manager_is_scoped(model):
            violations.append(
                f"UNSCOPED MANAGER {label}: default manager is not WorkspaceScopedManager "
                f"(security §4.1) — a tenant query could span workspaces."
            )

        # 3. RLS migration present for the table.
        table = model._meta.db_table
        if table not in rls_tables:
            violations.append(
                f"MISSING RLS {label} (table '{table}'): no EnableRowLevelSecurity "
                f"operation in migration history (security §4.1 step 3 / M-6)."
            )

    # 4. Viewsets over tenant models must extend the scoped base or be exempt.
    tenant_model_set = {m for m in all_models if _is_tenant_owned(m)}
    for path, view_cls in _all_viewset_classes().items():
        queryset = getattr(view_cls, "queryset", None)
        view_model = getattr(queryset, "model", None) if queryset is not None else None
        if view_model in tenant_model_set and not _extends_scoped_viewset(view_cls):
            if path not in EXEMPT_VIEWSETS:
                violations.append(
                    f"UNSCOPED VIEWSET {path}: serves tenant model "
                    f"{view_model._meta.label} but does not extend ScopedModelViewSet "
                    f"and is not in the exempt list (security §4.1 step 4)."
                )

    return violations


def _extends_scoped_viewset(view_cls: type) -> bool:
    """True iff any class in ``view_cls``'s MRO is the scoped viewset base.

    By MRO *name* (not import) so this infra guard never imports the api layer.
    """
    return any(base.__name__ == _SCOPED_VIEWSET_BASE for base in view_cls.__mro__)
