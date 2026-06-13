"""Swap the ``api_keys`` RLS policy from Class T to Class K (auth bootstrap).

The data-plane API-key auth flow (security §3.2: "Postgres api_keys by prefix")
must read a key row by its high-entropy ``key_prefix`` *before* any workspace
context exists — that lookup is how the request discovers the key's workspace.
Under the runtime NOBYPASSRLS role (SEC-TEN-2), the Class T policy
(``workspace_id = app_workspace_id()``) blocks that bootstrap read (no workspace
armed → default-deny → "unknown key" → spurious 401).

Class K keeps the workspace-scoped policy for every data path and adds a narrow
SELECT-only branch that admits exactly the row whose ``key_prefix`` equals the
presented credential prefix, set into ``app.api_key_prefix`` by
``ApiKeyAuthentication`` right before the lookup (tenancy.infra.rls ``_POLICY_K``).
A caller can therefore only ever read the one key row whose prefix they already
hold — never enumerate or read foreign keys — so RLS stays real.

Additive (the hard rule): 0001 created ``tenant_isolation`` on ``api_keys``; this
migration drops it and installs the Class K policies. Postgres-only; no-op on the
SQLite unit DB (RLS is a Postgres construct). The ``EnableRowLevelSecurity`` op
in 0001 still satisfies the ``check_tenancy`` "has an RLS migration" requirement
for ``api_keys`` — this only changes which policy is installed.
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

from tenancy.infra.rls import _POLICY_K, _POLICY_T  # reuse the policy SQL constants

_TABLE = "api_keys"

# Forward: drop Class T's policy, install Class K (workspace policy + bootstrap).
_FORWARD = 'DROP POLICY IF EXISTS tenant_isolation ON "{t}";\n' + _POLICY_K
# Reverse: drop Class K's policies, restore Class T.
_REVERSE = (
    'DROP POLICY IF EXISTS api_key_auth_bootstrap ON "{t}";\n'
    'DROP POLICY IF EXISTS api_key_access ON "{t}";\n'
) + _POLICY_T


class SwapApiKeyPolicy(migrations.RunSQL):
    """Replace ``api_keys``' Class T policy with Class K (Postgres-only)."""

    def __init__(self) -> None:
        super().__init__(
            sql=_FORWARD.format(t=_TABLE),
            reverse_sql=_REVERSE.format(t=_TABLE),
            elidable=False,
        )

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):

    dependencies = [
        ("tenancy", "0001_initial"),
    ]

    operations = [
        SwapApiKeyPolicy(),
    ]
