"""URL routing for the Tenancy API (api-spec §3 endpoint index #12-25, key-info).

Mounted under /api/v1 by config.urls. Workspace/member/key/quota/audit are the
JWT console surface; ``auth/key-info`` is the data-plane API-key probe.
"""

from django.urls import path

from tenancy.api import viewsets

urlpatterns = [
    # Workspaces (#12-16).
    path("workspaces", viewsets.WorkspaceCollectionView.as_view(), name="workspaces"),
    path(
        "workspaces/<str:workspace_id>",
        viewsets.WorkspaceDetailView.as_view(),
        name="workspace-detail",
    ),
    # Memberships (#17-20).
    path(
        "workspaces/<str:workspace_id>/members",
        viewsets.MembershipCollectionView.as_view(),
        name="workspace-members",
    ),
    path(
        "workspaces/<str:workspace_id>/members/<str:user_id>",
        viewsets.MembershipDetailView.as_view(),
        name="workspace-member-detail",
    ),
    # Quotas (#21).
    path(
        "workspaces/<str:workspace_id>/quotas",
        viewsets.QuotaView.as_view(),
        name="workspace-quotas",
    ),
    # API keys (#22-24).
    path(
        "workspaces/<str:workspace_id>/api-keys",
        viewsets.ApiKeyCollectionView.as_view(),
        name="workspace-api-keys",
    ),
    path(
        "workspaces/<str:workspace_id>/api-keys/<str:api_key_id>",
        viewsets.ApiKeyDetailView.as_view(),
        name="workspace-api-key-detail",
    ),
    # Audit log (#25).
    path(
        "workspaces/<str:workspace_id>/audit-log",
        viewsets.AuditLogView.as_view(),
        name="workspace-audit-log",
    ),
    # Key-info introspection (data-plane probe target, phase doc §27).
    path("auth/key-info", viewsets.KeyInfoView.as_view(), name="auth-key-info"),
]
