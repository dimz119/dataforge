"""TEN §7.2 — every OpenAPI route must be classified in the access-policy table.

The build-by-construction guarantee (TP-4): a new ``/api/v1`` endpoint that nobody
classified in ``access_policy.ACCESS_POLICY`` fails this test, so the cross-tenant
probe can never silently skip a surface. The inverse is also checked — a stale
policy entry for a route the schema no longer serves is flagged — so the table
tracks the code exactly.
"""

from __future__ import annotations

import pytest

from tests.tenancy.access_policy import ACCESS_POLICY
from tests.tenancy.route_enumerator import enumerate_api_v1_routes

pytestmark = pytest.mark.tenancy


def test_every_api_v1_route_is_classified() -> None:
    """Each enumerated ``(method, path)`` appears in the access-policy table."""
    routes = enumerate_api_v1_routes()
    assert routes, "route enumerator found no /api/v1 routes — schema generation broke"
    unclassified = sorted(str(r) for r in routes if r.key not in ACCESS_POLICY)
    assert not unclassified, (
        "Unclassified /api/v1 route(s) — add them to access_policy.ACCESS_POLICY "
        "with the correct RouteClass before they can ship:\n  " + "\n  ".join(unclassified)
    )


def test_no_stale_policy_entries() -> None:
    """Every policy entry corresponds to a live route (no drift the other way)."""
    live = {r.key for r in enumerate_api_v1_routes()}
    stale = sorted(f"{m} {p}" for (m, p) in ACCESS_POLICY if (m, p) not in live)
    assert not stale, (
        "access_policy.ACCESS_POLICY has entries for routes the schema no longer "
        "serves — remove them:\n  " + "\n  ".join(stale)
    )
