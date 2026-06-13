"""TEN §7.2 — the cross-tenant attack probe (permanent, unskippable).

For every ``/api/v1`` route x {foreign JWT, foreign API key, no credential},
substitute Workspace A's resource ids and authenticate as Workspace B, then assert
the security §3.3 outcome:

* object route → **404** (existence never confirmed, W-3 masking);
* sub-collection under a foreign workspace → **404**;
* scope-gated dual surface → **404** (foreign workspace masks before scope);
* auth-required own-data route → 2xx with B's OWN data only, or 401;
* public route → never 5xx, never an A-leak;
* key-probe → B's own workspace only;
* no credential → **401**.

Cardinal invariants on EVERY probe (SEC-AUTH-11): no 2xx carrying A's data, no
5xx, no ``permission-denied`` on a foreign object, and no A-sentinel anywhere in
the response body. A `permission-denied` (403) against a foreign object would
confirm existence — explicitly rejected here.

Routes and credential variants are enumerated at collection time, so a new
endpoint is probed by construction.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from rest_framework.response import Response

from tests.tenancy.access_policy import ACCESS_POLICY, RouteClass, expectations
from tests.tenancy.route_enumerator import Route, enumerate_api_v1_routes
from tests.tenancy.ten_fixture import (
    Tenant,
    client_for,
    credential_variants,
    substitute_path,
)

pytestmark = pytest.mark.tenancy

_VARIANTS = ("foreign_jwt", "foreign_key", "no_cred")

# (route, variant) product, built once at collection time.
_CASES: list[tuple[Route, str]] = [
    (route, variant) for route in enumerate_api_v1_routes() for variant in _VARIANTS
]


def _id(case: tuple[Route, str]) -> str:
    route, variant = case
    return f"{route.method}_{route.path}__{variant}"


def _body_text(response: Response) -> str:
    """Render the full response body to a string for sentinel scanning."""
    rendered: bytes = b""
    try:
        rendered = response.content
    except Exception:
        rendered = b""
    text = rendered.decode("utf-8", errors="replace") if rendered else ""
    data = getattr(response, "data", None)
    if data is not None:
        try:
            text += "\n" + json.dumps(data, default=str)
        except (TypeError, ValueError):
            text += "\n" + str(data)
    return text


def _send(client: Any, method: str, url: str) -> Response:
    """Issue ``method url`` with a minimal JSON body for write verbs."""
    verb = method.lower()
    fn = getattr(client, verb)
    if verb in ("post", "patch", "put"):
        # A throwaway body — the cross-tenant outcome must not depend on it.
        return cast(Response, fn(url, data={}, format="json"))
    return cast(Response, fn(url))


def _assert_no_sentinels(response: Response, victim: Tenant, url: str, label: str) -> None:
    """Scan the response body for A's sentinels.

    A's confidential values (name, slug, key material, email) are forbidden
    anywhere. A's *ids* are forbidden too, but only outside the request URL the
    attacker supplied — the RFC 9457 ``instance`` member legitimately mirrors the
    request path, so the request URL is stripped before the id scan.
    """
    body = _body_text(response)
    confidential = [s for s in victim.confidential_sentinels if s and s in body]
    assert not confidential, f"{label}: A confidential value(s) leaked: {confidential}"

    body_minus_url = body.replace(url, "")
    leaked_ids = [s for s in victim.id_sentinels if s and s in body_minus_url]
    assert not leaked_ids, f"{label}: A id(s) leaked outside the request path: {leaked_ids}"


@pytest.mark.parametrize("case", _CASES, ids=[_id(c) for c in _CASES])
def test_cross_tenant_probe(case: tuple[Route, str], victim: Tenant, attacker: Tenant) -> None:
    route, variant_name = case
    route_class = ACCESS_POLICY[route.key]
    variant = next(v for v in credential_variants(attacker) if v.name == variant_name)

    url = substitute_path(route.path, victim)
    client = client_for(variant)
    response = _send(client, route.method, url)
    status = response.status_code
    label = f"{route} [{variant_name}]"

    # 1. Never a server error on any probe — a 5xx is an isolation/robustness bug.
    assert status < 500, f"{label}: server error {status} (body={_body_text(response)[:300]})"

    # 2. A foreign object/collection/scope route must never confirm existence with
    #    a 403 permission-denied — that is the precise leak §3.3 forbids.
    if route_class in (RouteClass.OBJECT, RouteClass.COLLECTION, RouteClass.SCOPE):
        if variant_name in ("foreign_jwt", "foreign_key") and status == 403:
            pytest.fail(
                f"{label}: returned 403 permission-denied on a foreign resource — "
                "must be 404 to avoid confirming existence (§3.3, W-3)."
            )

    # 3. The expected status set for this (class, variant).
    expected = expectations(route_class)[variant_name]
    assert status in expected.statuses, (
        f"{label}: got {status}, expected one of {sorted(expected.statuses)} "
        f"for {route_class.value}/{variant_name} (body={_body_text(response)[:300]})"
    )

    # 4. No A-sentinel may appear in ANY response body, regardless of status.
    _assert_no_sentinels(response, victim, url, label)

    # 5. A 2xx is only acceptable on routes that legitimately return the caller's
    #    OWN data (already sentinel-scanned above); object/collection/scope must
    #    not 2xx under foreign credentials.
    if 200 <= status < 300 and not expected.allow_own_data:
        pytest.fail(f"{label}: unexpected 2xx ({status}) under foreign credential.")
