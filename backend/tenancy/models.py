"""Model discovery for the `tenancy` app.

Concrete models live in `tenancy.domain.models` (project-folder-structure §2.1)
and are re-exported here so Django's app registry finds them.
"""

from tenancy.domain.models import (
    ApiKey,
    Membership,
    UsageCounter,
    Workspace,
    WorkspaceInvitation,
    WorkspaceQuotas,
)

__all__ = [
    "ApiKey",
    "Membership",
    "UsageCounter",
    "Workspace",
    "WorkspaceInvitation",
    "WorkspaceQuotas",
]
