"""The two-workspace TEN attack fixture (testing-strategy §7.1).

Workspace **A** (victim) and Workspace **B** (attacker), each fully populated:
a verified admin user, its membership, and an API key covering every scope. A's
fixture plants sentinel values (its ids, name, key prefix) that the probe scans
for in every foreign response — any A-sentinel in a B-credentialed response body
is a leak (TP-4 / SEC-AUTH-11).

B's credentials are *valid*; only the targeted resources are foreign. The factory
is shared in shape with the E2E world (§16.4) so attack probes and browser tests
agree on the same two-tenant model.

This module is the data builder; ``conftest.py`` exposes it as pytest fixtures
and re-exports the base ``make_workspace`` factory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rest_framework.test import APIClient

from identity.domain.models import User
from identity.infra.jwt import issue_token_pair
from tenancy.application import keys as key_service
from tenancy.domain.context import workspace_context
from tenancy.domain.models import KEY_SCOPES, ROLE_ADMIN, ApiKey, Workspace

# Every minted TEN key carries the full non-admin scope set plus answer_key:read
# (granted by the admin actor) so scope-gated routes are reachable when the
# workspace matches — the probe still expects 404 cross-tenant (workspace masks
# before scope is checked).
TEN_KEY_SCOPES: list[str] = list(KEY_SCOPES)


@dataclass(frozen=True)
class Tenant:
    """A fully populated workspace + its admin, JWT, and a live API key."""

    workspace: Workspace
    admin: User
    api_key: ApiKey
    api_key_plaintext: str
    access_token: str

    @property
    def confidential_sentinels(self) -> tuple[str, ...]:
        """A's private values that must NEVER appear in a foreign response body.

        Excludes the workspace/user/key *ids* the attacker substitutes into the
        request URL — those legitimately echo in the RFC 9457 ``instance`` member
        (it mirrors the request path), so the probe strips the request URL before
        scanning (see ``test_cross_tenant_probes``). These are A's confidential
        payload: a name, slug, key material, or email surfacing under B's
        credentials is an unambiguous leak.
        """
        return (
            self.workspace.name,
            self.workspace.slug,
            self.api_key.key_prefix,
            self.api_key.last4,
            self.api_key_plaintext,
            self.admin.email,
        )

    @property
    def id_sentinels(self) -> tuple[str, ...]:
        """A's ids — leaks only if they appear OUTSIDE the request path the
        attacker themselves supplied (the probe removes the URL before scanning).
        """
        return (
            str(self.workspace.id),
            str(self.api_key.id),
            str(self.admin.id),
        )


def build_tenant(*, make_user, label: str) -> Tenant:  # type: ignore[no-untyped-def]
    """Build one fully populated tenant (workspace + admin + key)."""
    from tenancy.application import services

    admin = make_user(f"{label}@example.com", is_verified=True)
    workspace = services.create_workspace(user=admin, name=f"Workspace {label}", slug=None)
    with workspace_context(workspace.id):
        api_key, plaintext = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name=f"{label}-key",
            scopes=TEN_KEY_SCOPES,
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
    token = issue_token_pair(admin)
    return Tenant(
        workspace=workspace,
        admin=admin,
        api_key=api_key,
        api_key_plaintext=plaintext,
        access_token=str(token.access_token),
    )


@dataclass
class CredentialVariant:
    """One way to authenticate a probe request against a foreign resource."""

    name: str  # "foreign_jwt" | "foreign_key" | "no_cred"
    headers: dict[str, str] = field(default_factory=dict)


def credential_variants(attacker: Tenant) -> list[CredentialVariant]:
    """The three probe credential variants from B against A (testing §7.2)."""
    return [
        CredentialVariant(
            "foreign_jwt", {"HTTP_AUTHORIZATION": f"Bearer {attacker.access_token}"}
        ),
        CredentialVariant("foreign_key", {"HTTP_X_API_KEY": attacker.api_key_plaintext}),
        CredentialVariant("no_cred", {}),
    ]


def substitute_path(path: str, victim: Tenant) -> str:
    """Fill an OpenAPI path template with Workspace A's real ids.

    ``{workspace_id}`` → A's workspace; ``{user_id}`` → A's admin; ``{api_key_id}``
    → A's key. Any other id param falls back to A's workspace id (a foreign id
    must still mask to 404).
    """
    return (
        path.replace("{workspace_id}", str(victim.workspace.id))
        .replace("{user_id}", str(victim.admin.id))
        .replace("{api_key_id}", str(victim.api_key.id))
    )


def client_for(variant: CredentialVariant) -> APIClient:
    """A fresh APIClient carrying ``variant``'s credentials (no leakage)."""
    client = APIClient()
    if variant.headers:
        client.credentials(**variant.headers)
    return client


__all__ = [
    "TEN_KEY_SCOPES",
    "CredentialVariant",
    "Tenant",
    "build_tenant",
    "client_for",
    "credential_variants",
    "substitute_path",
]
