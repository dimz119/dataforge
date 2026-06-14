"""DB-backed fixtures for the Generation (batch datasets) tests (Phase 4).

Builds a workspace with the published e-commerce builtin scenario + a pinned
scenario instance whose overlay shrinks the seeding catalogs (users 100 /
products 50, within the §5 min/max) so a sync backfill batch is fast. The dataset
service then drives the real generic engine end to end.

These fixtures arm the workspace context for direct service-layer calls (the
service uses the scoped manager), mirroring how the request middleware arms it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_BUILTIN = (
    Path(__file__).resolve().parents[2]
    / "catalog"
    / "builtin"
    / "ecommerce"
    / "1.0.0.yaml"
)
# Small catalog overlay so the sync batch stays well under the 50k sync threshold.
SMALL_CATALOGS = {"catalog_sizes": {"users": 100, "products": 50}}


@dataclass
class WorkspaceFixture:
    """A workspace + admin + a pinned scenario instance, context-armed."""

    workspace: Any
    admin: Any
    instance: Any


def _publish_ecommerce() -> Any:
    """Ingest + publish the ecommerce builtin as a global scenario (L1+L2+derive)."""
    from catalog.application import ingest, publish

    text = _BUILTIN.read_text(encoding="utf-8")
    draft = ingest.create_draft(
        text, workspace_id=None, is_workspace_visibility=False, builtin=True
    )
    return publish.publish_manifest_version(draft, actor="system", workspace_id=None)


@pytest.fixture
def gen_workspace(db: Any) -> WorkspaceFixture:
    """A workspace with a pinned ecommerce instance (small catalogs), context armed.

    Seeds a *global* scenario, so this fixture runs in the publish-path (owner) lane
    (database-schema §9.6: writing NULL-workspace rows is owner-only). RLS-negative
    ledger assertions that must run under the non-bypassing ``dataforge_app`` role
    use :func:`gen_ledger_ws` instead (no global seeding).
    """
    from catalog.application import services as catalog_services
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    _publish_ecommerce()
    admin = User.objects.create_user(email="gen-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Gen Lab", slug=None)
    ws_context.activate(workspace.id)
    instance = catalog_services.create_instance(
        workspace=workspace,
        name="ecommerce-batch",
        scenario_slug="ecommerce",
        manifest_version="1.0.0",
        configuration=SMALL_CATALOGS,
        default_seed=42,
        actor=admin,
    )
    return WorkspaceFixture(workspace=workspace, admin=admin, instance=instance)


@pytest.fixture
def gen_ledger_ws(db: Any) -> WorkspaceFixture:
    """A workspace + admin only — NO global scenario (runtime-role-safe).

    The ledger RLS-negative tests insert envelopes directly (the ``_envelope``
    helper) and assert a foreign/unset GUC sees zero rows; that assertion only bites
    under the non-bypassing ``dataforge_app`` role (a superuser bypasses RLS). Those
    tests therefore run in the isolation lane, where seeding a global scenario would
    be rejected — so this fixture creates only the tenant workspace (the production
    create flow arms the GUC so the Class-W WITH CHECK passes under the runtime role).
    """
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    admin = User.objects.create_user(
        email="gen-ledger-admin@example.com", password="pw-correct-horse"
    )
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Ledger Lab", slug=None)
    ws_context.activate(workspace.id)
    return WorkspaceFixture(workspace=workspace, admin=admin, instance=None)
