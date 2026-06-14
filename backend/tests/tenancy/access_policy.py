"""The access-policy classification table for the cross-tenant attack suite.

The TEN suite (testing-strategy §7.2) auto-enrolls *every* ``(method, path)`` in
the generated OpenAPI schema. Each route must be classified here; an unclassified
route fails the build by construction (TP-4 — "a new endpoint that nobody
classified fails the build"). The classification fixes the expected outcome under
foreign credentials per the security §3.3 401/403/404 policy table.

Route classes (security §3.3, SEC-AUTH-11):

* ``PUBLIC``       — no auth required; reachable with no credential (signup,
  login, refresh, password-reset request/confirm, verify-email). Foreign / no
  credential must still NOT 5xx and must not leak A's data. These accept a body;
  the probe sends an empty/garbage body and accepts any non-5xx, non-leaking
  status (typically 400 validation-error or 401 auth-failed).
* ``AUTH``         — requires a valid principal but is not workspace-scoped to a
  path id (``/workspaces`` collection, ``/users/me``). With a foreign *valid*
  JWT the route returns 200 with only the *caller's own* data (never A's), or 401
  with no credential. The probe asserts no A-sentinel leaks and no 5xx.
* ``OBJECT``       — a workspace/resource id in the path is the discriminator. A
  foreign credential against A's id must return **404** (existence never
  confirmed, W-3 masking) — never 403, never 2xx-with-A-data. No credential →
  401.
* ``COLLECTION``   — a sub-collection under a workspace id (``/members``,
  ``/api-keys``, ``/audit-log``). Foreign credential → 404 (the parent workspace
  is foreign, so the membership lookup masks it). No credential → 401.
* ``SCOPE``        — a dual JWT|key surface gated by an API-key scope
  (``/quotas``). A foreign key for A's workspace → 404 (foreign workspace
  masked, W-1); a no-credential request → 401. (Insufficient-scope-within-own
  -workspace → 403 is exercised by the unit suite; the cross-tenant probe never
  reaches a 403 because the foreign workspace masks first.)
* ``KEY_PROBE``    — ``/auth/key-info``: the API-key-only data-plane probe. A
  foreign valid key returns *its own* workspace (never A's). The probe asserts
  the response never contains A's sentinels; no credential / a JWT → 401.

Per-route expected status sets under each credential variant are derived from
the class in ``expectations`` so the parametrized probe stays declarative.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RouteClass(StrEnum):
    PUBLIC = "public"
    AUTH = "auth"  # auth-required, not path-scoped (own-data collection)
    OBJECT = "object"  # path id is the discriminator → 404 foreign
    COLLECTION = "collection"  # sub-collection under a workspace id → 404 foreign
    SCOPE = "scope"  # dual JWT|key, scope-gated
    KEY_PROBE = "key_probe"  # API-key-only introspection probe
    # Global-readable hybrid surface (catalog scenarios, registry subjects): JWT or
    # key (any/read scope) both return globals + the caller's OWN data only — never
    # A's workspace-private rows. The path segment (slug/subject) is not a workspace
    # id, so an unknown literal masks to 404; a 200 carries only globals/own data.
    GLOBAL_READ = "global_read"
    # JWT-only catalog write surface (draft create, publish): a key is the wrong
    # credential → 401; a foreign JWT against a foreign slug/version → 404, or a
    # 400/422 on the throwaway body. Never 2xx-with-A-data.
    CATALOG_WRITE = "catalog_write"


@dataclass(frozen=True)
class CredentialExpectation:
    """Allowed status codes for one credential variant against a route.

    ``allow_own_data`` marks routes where a *valid* foreign principal legitimately
    receives a 2xx carrying ITS OWN data (never A's) — the suite then asserts the
    body is free of A-sentinels rather than forbidding 2xx outright.
    """

    statuses: frozenset[int]
    allow_own_data: bool = False


# The closed access-policy table: every (METHOD, normalized-path) → RouteClass.
# Paths use the OpenAPI template form (``{workspace_id}`` etc.). The enumerator
# normalizes the live schema's path templates to this form before lookup.
ACCESS_POLICY: dict[tuple[str, str], RouteClass] = {
    # --- Public auth surfaces (no credential required) -----------------------
    ("POST", "/api/v1/auth/signup"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/verify-email"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/resend-verification"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/login"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/refresh"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/password-reset"): RouteClass.PUBLIC,
    ("POST", "/api/v1/auth/password-reset/confirm"): RouteClass.PUBLIC,
    # logout reads the refresh cookie; no cookie → still must not 5xx/leak.
    ("POST", "/api/v1/auth/logout"): RouteClass.PUBLIC,
    # --- Auth-required, own-data (not path-scoped to a foreign id) ------------
    ("GET", "/api/v1/workspaces"): RouteClass.AUTH,
    ("POST", "/api/v1/workspaces"): RouteClass.AUTH,
    ("GET", "/api/v1/users/me"): RouteClass.AUTH,
    ("DELETE", "/api/v1/users/me"): RouteClass.AUTH,
    ("POST", "/api/v1/users/me/password"): RouteClass.AUTH,
    # --- Object routes (path workspace id is the discriminator → 404 foreign) -
    ("GET", "/api/v1/workspaces/{workspace_id}"): RouteClass.OBJECT,
    ("PATCH", "/api/v1/workspaces/{workspace_id}"): RouteClass.OBJECT,
    ("DELETE", "/api/v1/workspaces/{workspace_id}"): RouteClass.OBJECT,
    ("PATCH", "/api/v1/workspaces/{workspace_id}/members/{user_id}"): RouteClass.OBJECT,
    ("DELETE", "/api/v1/workspaces/{workspace_id}/members/{user_id}"): RouteClass.OBJECT,
    ("DELETE", "/api/v1/workspaces/{workspace_id}/api-keys/{api_key_id}"): RouteClass.OBJECT,
    # --- Sub-collections under a workspace id (foreign parent → 404) ----------
    ("GET", "/api/v1/workspaces/{workspace_id}/members"): RouteClass.COLLECTION,
    ("POST", "/api/v1/workspaces/{workspace_id}/members"): RouteClass.COLLECTION,
    ("GET", "/api/v1/workspaces/{workspace_id}/api-keys"): RouteClass.COLLECTION,
    ("POST", "/api/v1/workspaces/{workspace_id}/api-keys"): RouteClass.COLLECTION,
    ("GET", "/api/v1/workspaces/{workspace_id}/audit-log"): RouteClass.COLLECTION,
    # --- Scope-gated dual surface --------------------------------------------
    ("GET", "/api/v1/workspaces/{workspace_id}/quotas"): RouteClass.SCOPE,
    # --- API-key-only data-plane probe ---------------------------------------
    ("GET", "/api/v1/auth/key-info"): RouteClass.KEY_PROBE,
    # --- Catalog: scenario reads (globals + caller's own; #26-29) ------------
    ("GET", "/api/v1/scenarios"): RouteClass.GLOBAL_READ,
    ("GET", "/api/v1/scenarios/{scenario_slug}"): RouteClass.GLOBAL_READ,
    ("GET", "/api/v1/scenarios/{scenario_slug}/versions"): RouteClass.GLOBAL_READ,
    (
        "GET",
        "/api/v1/scenarios/{scenario_slug}/versions/{manifest_version}",
    ): RouteClass.GLOBAL_READ,
    # Validation poll is JWT-only + slug-resolved (#30): foreign slug → 404, key → 401.
    (
        "GET",
        "/api/v1/scenarios/{scenario_slug}/versions/{manifest_version}/validation",
    ): RouteClass.OBJECT,
    # --- Catalog: scenario writes (JWT-only; #31-32) -------------------------
    ("POST", "/api/v1/scenarios"): RouteClass.CATALOG_WRITE,
    (
        "POST",
        "/api/v1/scenarios/{scenario_slug}/versions/{manifest_version}/publish",
    ): RouteClass.CATALOG_WRITE,
    # --- Catalog: scenario instances under a workspace id (#33-38) -----------
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/scenario-instances",
    ): RouteClass.SCOPE,  # dual JWT|Key(streams:read); foreign workspace masks → 404
    (
        "POST",
        "/api/v1/workspaces/{workspace_id}/scenario-instances",
    ): RouteClass.COLLECTION,  # JWT member-only; key → 401
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}",
    ): RouteClass.SCOPE,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}",
    ): RouteClass.COLLECTION,  # JWT member-only; key → 401
    (
        "GET",
        "/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}/configuration",
    ): RouteClass.SCOPE,
    (
        "PUT",
        "/api/v1/workspaces/{workspace_id}/scenario-instances/{scenario_instance_id}/configuration",
    ): RouteClass.COLLECTION,  # JWT member-only; key → 401
    # --- Datasets: backfill batch generation (api-spec §4.10 #57-61) ---------
    # Dual JWT|Key surfaces; the owning workspace (body workspace_id for POST/list,
    # the key's own workspace, or the dataset's workspace for {id}) masks foreign
    # access to 404 for both credential types (W-1/W-3). A no-credential request →
    # 401. SCOPE captures exactly this: foreign_jwt → 404, foreign_key → 404,
    # no_cred → 401.
    ("POST", "/api/v1/datasets"): RouteClass.SCOPE,
    ("GET", "/api/v1/datasets"): RouteClass.SCOPE,
    ("GET", "/api/v1/datasets/{dataset_id}"): RouteClass.SCOPE,
    ("GET", "/api/v1/datasets/{dataset_id}/download"): RouteClass.SCOPE,
    ("DELETE", "/api/v1/datasets/{dataset_id}"): RouteClass.SCOPE,
    # --- Streams: control-plane lifecycle (api-spec §4.8 #39-44) -------------
    # Dual JWT|Key surfaces; the owning workspace (body workspace_id for POST/list,
    # the key's own workspace, or the stream's workspace for {id}) masks foreign
    # access to 404 for both credential types (W-1/W-3). No credential → 401. SCOPE
    # captures exactly this: foreign_jwt → 404, foreign_key → 404, no_cred → 401.
    ("POST", "/api/v1/streams"): RouteClass.SCOPE,
    ("GET", "/api/v1/streams"): RouteClass.SCOPE,
    ("GET", "/api/v1/streams/{stream_id}"): RouteClass.SCOPE,
    ("POST", "/api/v1/streams/{stream_id}/start"): RouteClass.SCOPE,
    ("POST", "/api/v1/streams/{stream_id}/stop"): RouteClass.SCOPE,
    # --- Delivery: REST cursor pull (api-spec §4.9.1; delivery-channels §5) ---
    # Dual JWT|Key(events:read) data-plane read over event_buffer; the stream's
    # owning workspace masks foreign access to 404 for both credential types (W-1/
    # RC-5). No credential → 401. SCOPE captures foreign_jwt→404, foreign_key→404,
    # no_cred→401. (Insufficient events:read within own workspace → 403 is the unit
    # suite's job; the cross-tenant probe never reaches it — the workspace masks first.)
    ("GET", "/api/v1/streams/{stream_id}/events"): RouteClass.SCOPE,
    # --- Registry: schema reads (globals + caller's own; #62-65) -------------
    ("GET", "/api/v1/schemas"): RouteClass.GLOBAL_READ,
    ("GET", "/api/v1/schemas/{subject}"): RouteClass.GLOBAL_READ,
    ("GET", "/api/v1/schemas/{subject}/versions"): RouteClass.GLOBAL_READ,
    ("GET", "/api/v1/schemas/{subject}/versions/{schema_version}"): RouteClass.GLOBAL_READ,
}


# Per (class, credential-variant) → allowed outcome. Variants:
#   "foreign_jwt"  — B's valid console JWT against A's resources
#   "foreign_key"  — B's valid API key against A's resources
#   "no_cred"      — no Authorization, no X-API-Key
#
# The cardinal rule (SEC-AUTH-11): never 2xx-with-A-data, never 5xx, never
# `permission-denied` on a foreign object (that would confirm existence).
def expectations(route_class: RouteClass) -> dict[str, CredentialExpectation]:
    if route_class is RouteClass.PUBLIC:
        # No auth contract; the route accepts a body. With garbage/empty input it
        # may 400 (validation), 401 (auth-failed), 200/201/205 (idempotent ok),
        # or 404 (token-not-found). The hard guarantee is: no 5xx, no A-leak.
        ok = CredentialExpectation(
            frozenset({200, 201, 204, 205, 400, 401, 404, 409, 422}), allow_own_data=True
        )
        return {"foreign_jwt": ok, "foreign_key": ok, "no_cred": ok}
    if route_class is RouteClass.AUTH:
        return {
            # Valid foreign principal: 2xx with its OWN data only (never A's), or a
            # 400/403 (e.g. unverified create) — the body must carry no A-sentinel.
            "foreign_jwt": CredentialExpectation(
                frozenset({200, 201, 400, 403, 404, 409}), allow_own_data=True
            ),
            # A key on a JWT-only surface is an absent credential → 401.
            "foreign_key": CredentialExpectation(frozenset({401})),
            "no_cred": CredentialExpectation(frozenset({401})),
        }
    if route_class in (RouteClass.OBJECT, RouteClass.COLLECTION):
        return {
            "foreign_jwt": CredentialExpectation(frozenset({404})),
            # A key on the JWT-only console surface is absent → 401.
            "foreign_key": CredentialExpectation(frozenset({401})),
            "no_cred": CredentialExpectation(frozenset({401})),
        }
    if route_class is RouteClass.SCOPE:
        return {
            "foreign_jwt": CredentialExpectation(frozenset({404})),
            # Foreign key for A's workspace → 404 (foreign workspace masked, W-1).
            "foreign_key": CredentialExpectation(frozenset({404})),
            "no_cred": CredentialExpectation(frozenset({401})),
        }
    if route_class is RouteClass.GLOBAL_READ:
        # Globals + the caller's OWN data only. A collection (no path id) → 200
        # carrying globals/own data; a resource read with a literal foreign
        # slug/subject → 404 (the literal is unknown to B). Never A's private rows.
        own = CredentialExpectation(frozenset({200, 404}), allow_own_data=True)
        return {"foreign_jwt": own, "foreign_key": own, "no_cred": CredentialExpectation(
            frozenset({401})
        )}
    if route_class is RouteClass.CATALOG_WRITE:
        return {
            # Foreign JWT with a throwaway body / foreign slug: 400 (bad body),
            # 404 (foreign slug), 403 (not admin), 409/422 — never 2xx-with-A-data.
            "foreign_jwt": CredentialExpectation(frozenset({400, 403, 404, 409, 422})),
            # A key on the JWT-only write surface is an absent credential → 401.
            "foreign_key": CredentialExpectation(frozenset({401})),
            "no_cred": CredentialExpectation(frozenset({401})),
        }
    # KEY_PROBE: API-key-only. A foreign key gets ITS OWN workspace back (200,
    # never A's). A JWT here is the wrong credential type → 401; no cred → 401.
    return {
        "foreign_jwt": CredentialExpectation(frozenset({401})),
        "foreign_key": CredentialExpectation(frozenset({200}), allow_own_data=True),
        "no_cred": CredentialExpectation(frozenset({401})),
    }


__all__ = [
    "ACCESS_POLICY",
    "CredentialExpectation",
    "RouteClass",
    "expectations",
]
