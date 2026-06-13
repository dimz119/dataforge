"""Reusable identity permissions / predicates (INV-ID-2).

The tenancy app applies these to gate every tenant-creating command (workspace
create, invitation accept, API-key create) behind `is_verified = true`
(security §5.1, A-6). Exported here so the gate lives in one place and the
tenancy agent imports it rather than re-deriving it.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from config.problems import EmailNotVerified


def is_verified(user: Any) -> bool:
    """True iff `user` is an authenticated, verified, non-tombstoned account.

    The single predicate behind INV-ID-2. Reads the row's `is_verified`, never a
    token claim, so verification takes effect immediately (security §3.1.2).
    """
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_verified", False)
        and getattr(user, "deleted_at", None) is None
    )


def require_verified(user: Any) -> None:
    """Raise 403 `email-not-verified` unless `user` is verified (INV-ID-2).

    The imperative form for service/view code that is not a DRF permission
    (e.g. inside a transaction performing the tenant-creating mutation).
    """
    if not is_verified(user):
        raise EmailNotVerified()


class IsVerified(BasePermission):
    """DRF permission: authenticated **and** email-verified (INV-ID-2, A-6).

    On failure raises 403 `email-not-verified` (not the default 403) so the
    response slug matches the §3.3 policy. Compose with `IsAuthenticated`
    (authentication failures must still surface as 401).
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False  # let IsAuthenticated/authentication yield the 401
        require_verified(user)
        return True
