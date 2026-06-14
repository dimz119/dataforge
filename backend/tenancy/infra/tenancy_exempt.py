"""The closed exempt list for the ``check_tenancy`` guard (security §4.1 step 1).

Classification is *closed*: every installed model is either (a) tenant-owned
(subclasses ``WorkspaceScopedModel`` with a ``workspace_id`` field, the scoped
manager, and an RLS migration) or (b) listed here with a one-line justification.
An unclassified model fails the guard.

Entries are ``"app_label.ModelName"`` (the model's ``label`` lower/Pascal mix
as Django reports it via ``model._meta.label``). Justifications cite the
database-schema §9.6 reasoning.

Viewset exemptions (auth endpoints, health probes, account pages) are listed
separately — those APIViews legitimately do not extend ``ScopedModelViewSet``.
"""

from __future__ import annotations

# Models that are deliberately NOT tenant-owned (database-schema §9.6).
EXEMPT_MODELS: dict[str, str] = {
    # Identity is workspace-agnostic by design (§9.6): accounts exist before any
    # workspace and pre-auth flows must read them before any context exists.
    "identity.User": "Identity is workspace-agnostic (database-schema §9.6).",
    "identity.UserToken": "Verification/reset tokens belong to accounts, not workspaces (§9.6).",
    # Workspaces is self-tenant-owned: its id IS the tenant id (§9.4), policed by
    # RLS Class W + membership checks, not by a workspace_id column.
    "tenancy.Workspace": "Self-tenant-owned: PK is the tenant id (database-schema §9.4, Class W).",
    # SimpleJWT blacklist storage — account/session tokens, no tenant data (§9.6).
    "token_blacklist.OutstandingToken": "Session-token storage; not tenant data (§9.6).",
    "token_blacklist.BlacklistedToken": "Session-token storage; not tenant data (§9.6).",
    # Django framework tables — no tenant data (§9.6).
    "auth.Permission": "Django framework table; no tenant data (§9.6).",
    "auth.Group": "Django framework table; no tenant data (§9.6).",
    "contenttypes.ContentType": "Django framework table; no tenant data (§9.6).",
    # Audit is hybrid (nullable workspace_id, §9.5): owned by the Audit app under
    # RLS Class A, not the Class T scoped-manager pattern. Exempt from the
    # tenant-model assertions; the Audit app owns its enforcement.
    "audit.AuditEntry": "Audit hybrid: nullable workspace_id, RLS Class A (§9.5); Audit-app owned.",
    "audit.AuditLog": "Audit hybrid: nullable workspace_id, RLS Class A (§9.5); Audit-app owned.",
    # Catalog scenarios + manifest versions are hybrid (nullable workspace_id,
    # §9.5): global (NULL) builtin rows are world-readable; workspace rows are
    # tenant-owned. RLS Class H (catalog.infra.rls), not the Class T scoped
    # manager. The Catalog app owns its own enforcement. (database-schema §9.6.)
    "catalog.Scenario": "Catalog hybrid: nullable workspace_id, RLS Class H (§9.5); app-owned.",
    "catalog.ManifestVersion": "Catalog hybrid: nullable workspace_id, RLS Class H; app-owned.",
    # Registry subjects + versions are hybrid (nullable workspace_id, §9.5):
    # global (NULL) builtin subjects must resolve for every workspace's envelopes
    # (INV-REG-4). RLS Class H (registry.infra.rls), not Class T. Registry-app owned.
    "registry.Subject": "Registry hybrid: nullable workspace_id, RLS Class H (§9.5); app-owned.",
    "registry.SchemaVersion": "Registry hybrid: nullable workspace_id, RLS Class H; app-owned.",
}

# DRF viewsets that legitimately do NOT extend ``ScopedModelViewSet``
# (auth endpoints, health probes, account pages — security §4.1 step 4). Keyed
# by the view class's import path.
EXEMPT_VIEWSETS: frozenset[str] = frozenset(
    {
        # Identity auth/account APIViews (no tenant data; JWT issuance/account ops).
        "identity.api.viewsets.SignupView",
        "identity.api.viewsets.VerifyEmailView",
        "identity.api.viewsets.ResendVerificationView",
        "identity.api.viewsets.LoginView",
        "identity.api.viewsets.RefreshView",
        "identity.api.viewsets.LogoutView",
        "identity.api.viewsets.PasswordResetRequestView",
        "identity.api.viewsets.PasswordResetConfirmView",
        "identity.api.viewsets.UserMeView",
        "identity.api.viewsets.ChangePasswordView",
        # Workspace create/list + membership/key/audit management run on the
        # workspace COLLECTION (the workspace is the tenant) or self-tenant-owned
        # Workspace — they apply membership/role/scope perms directly rather than
        # the row-scoping queryset base. They are workspace-context-armed and
        # cross-tenant-probed by the TEN suite; exempt from the row-scoped base.
        "tenancy.api.viewsets.WorkspaceCollectionView",
        "tenancy.api.viewsets.WorkspaceDetailView",
        "tenancy.api.viewsets.MembershipCollectionView",
        "tenancy.api.viewsets.MembershipDetailView",
        "tenancy.api.viewsets.ApiKeyCollectionView",
        "tenancy.api.viewsets.ApiKeyDetailView",
        "tenancy.api.viewsets.QuotaView",
        "tenancy.api.viewsets.AuditLogView",
        "tenancy.api.viewsets.KeyInfoView",
    }
)

# ``all_objects`` use-site allowlist (security §4.1 step 5). Each entry is a
# ``module:reason`` the guard reconciles against the marked call sites.
ALL_OBJECTS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "tenancy.application.services",  # workspace-creation + cross-workspace listing
        "tenancy.application.keys",  # prefix lookup precedes workspace context
        "tenancy.domain.scoping",  # the escape-hatch managers themselves
    }
)
