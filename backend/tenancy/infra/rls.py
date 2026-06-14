"""Layer 2 DDL: custom migration operations for Postgres Row-Level Security.

``EnableRowLevelSecurity`` is the operation the ``check_tenancy`` guard looks
for in every tenant table's migration history (security §4.1 step 3): it
``ENABLE``s + ``FORCE``s RLS and installs the table's policy class
(database-schema §9.5). On non-Postgres backends (the SQLite test DB) the SQL is
a no-op — RLS is a Postgres construct; Layer 1 + the CI raw-SQL probes carry the
guarantee there.

Policy classes (database-schema §9.5):

* ``T`` — standard tenant table: ``workspace_id = app_workspace_id()`` for both
  USING and WITH CHECK.
* ``W`` — ``workspaces`` itself (PK is the tenant id; plus own-workspace listing
  via the memberships probe).
* ``M`` — ``memberships`` (workspace-scoped plus "my memberships" across
  workspaces).

The null-safe accessor functions ``app_workspace_id()`` / ``app_user_id()`` are
created once by ``CreateGucAccessors`` (database-schema §9.3).
"""

from __future__ import annotations

from typing import Any

from django.db import migrations
from django.db.migrations.state import ProjectState

# database-schema §9.3 — null-safe GUC accessors.
_CREATE_ACCESSORS = """
CREATE OR REPLACE FUNCTION app_workspace_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$;
CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
"""
_DROP_ACCESSORS = """
DROP FUNCTION IF EXISTS app_workspace_id();
DROP FUNCTION IF EXISTS app_user_id();
"""

# backend-architecture §4.2 / §8.3 — the platform-read accessor. The runner data
# plane is a platform process spanning EVERY workspace's shards: its per-tick
# claimable scan and per-shard desired/checkpoint reads cross tenants by design
# (INV-STR-6), and the flat single-resource API routes must resolve a resource's
# owning workspace BEFORE any workspace context can be armed. Both are pre-context
# cross-tenant SELECTs that the strict Class T `workspace_id = app_workspace_id()`
# policy hides from the NOBYPASSRLS runtime role. ``app.platform`` is a narrow,
# transaction-local opt-in (mirroring the ``app.api_key_prefix`` auth-bootstrap
# precedent) honoured ONLY by the Class T USING (read) clause — WITH CHECK stays
# strictly workspace-scoped, so writes still require a real armed workspace and
# cross-tenant writes remain impossible. Set exclusively by trusted platform code
# (``guc.set_platform_guc`` via ``platform_read_scope``); no data endpoint sets it.
_CREATE_PLATFORM_ACCESSOR = """
CREATE OR REPLACE FUNCTION app_is_platform() RETURNS boolean
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT coalesce(current_setting('app.platform', true), '') = 'on' $$;
"""
_DROP_PLATFORM_ACCESSOR = "DROP FUNCTION IF EXISTS app_is_platform();"

_ENABLE = (
    'ALTER TABLE "{t}" ENABLE ROW LEVEL SECURITY;\n'
    'ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY;'
)
_DISABLE = (
    'ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY;\n'
    'ALTER TABLE "{t}" DISABLE ROW LEVEL SECURITY;'
)

# Class T — standard tenant table (database-schema §9.5). The USING (read) clause
# also admits the platform data plane (backend-architecture §4.2 / §8.3): the
# runner's cross-tenant claimable scan + the flat-route pre-arm workspace resolve.
# WITH CHECK stays strictly workspace-scoped — platform reads can never become
# cross-tenant writes (a write still requires a real armed workspace).
_POLICY_T = """
CREATE POLICY tenant_isolation ON "{t}" FOR ALL
    USING (workspace_id = app_workspace_id() OR app_is_platform())
    WITH CHECK (workspace_id = app_workspace_id());
"""
# Class W — workspaces (PK is the tenant id; own-workspace listing via memberships).
_POLICY_W = """
CREATE POLICY workspace_self ON "{t}" FOR ALL
    USING (id = app_workspace_id()
           OR EXISTS (SELECT 1 FROM memberships m
                      WHERE m.workspace_id = "{t}".id AND m.user_id = app_user_id()))
    WITH CHECK (id = app_workspace_id());
"""
# Class M — memberships (workspace-scoped plus "my memberships" across workspaces).
_POLICY_M = """
CREATE POLICY membership_access ON "{t}" FOR ALL
    USING (workspace_id = app_workspace_id() OR user_id = app_user_id())
    WITH CHECK (workspace_id = app_workspace_id());
"""
# Class K — api_keys. Workspace-scoped like Class T for every data path, PLUS a
# narrow authentication-bootstrap branch: a SELECT may read the single row whose
# high-entropy ``key_prefix`` equals the presented credential prefix (set into the
# transaction-local ``app.api_key_prefix`` GUC by ApiKeyAuthentication before the
# lookup). The prefix lookup is how the data-plane auth flow discovers a key's
# workspace before any workspace context exists (security §3.2 flow: "Postgres
# api_keys by prefix"); the row is then secret-hash-compared. This keeps RLS real
# for the runtime NOBYPASSRLS role (SEC-TEN-2): a caller can only ever read the
# exact key row whose prefix they already hold — never enumerate or read foreign
# keys — and no data endpoint sets ``app.api_key_prefix``, so the workspace branch
# alone gates all non-auth access. WITH CHECK stays workspace-scoped (key creation
# always runs with the workspace armed); the bootstrap branch is SELECT-only.
_POLICY_K = """
CREATE POLICY api_key_access ON "{t}" FOR ALL
    USING (workspace_id = app_workspace_id())
    WITH CHECK (workspace_id = app_workspace_id());
CREATE POLICY api_key_auth_bootstrap ON "{t}" FOR SELECT
    USING (key_prefix = nullif(current_setting('app.api_key_prefix', true), ''));
"""
_POLICIES = {"T": _POLICY_T, "W": _POLICY_W, "M": _POLICY_M, "K": _POLICY_K}
_DROP_POLICY = {
    "T": 'DROP POLICY IF EXISTS tenant_isolation ON "{t}";',
    "W": 'DROP POLICY IF EXISTS workspace_self ON "{t}";',
    "M": 'DROP POLICY IF EXISTS membership_access ON "{t}";',
    "K": (
        'DROP POLICY IF EXISTS api_key_auth_bootstrap ON "{t}";\n'
        'DROP POLICY IF EXISTS api_key_access ON "{t}";'
    ),
}


class CreateGucAccessors(migrations.RunSQL):
    """Create the null-safe ``app_workspace_id()`` / ``app_user_id()`` functions.

    Also creates ``app_is_platform()`` (backend-architecture §4.2) so a fresh DB
    has the platform-read accessor the Class T USING clause references.
    """

    def __init__(self) -> None:
        super().__init__(
            sql=_CREATE_ACCESSORS + _CREATE_PLATFORM_ACCESSOR,
            reverse_sql=_DROP_PLATFORM_ACCESSOR + _DROP_ACCESSORS,
            elidable=False,
        )

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return  # RLS accessors are Postgres-only
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)


class EnableRowLevelSecurity(migrations.RunSQL):
    """Enable + force RLS on ``table`` and install its ``policy_class`` policy.

    The marker operation the ``check_tenancy`` guard requires in every tenant
    table's migration history (security §4.1 step 3 / M-6). The model label is
    recorded on the instance so the guard can map operations to tables.
    """

    def __init__(self, *, table: str, policy_class: str = "T", model_label: str = "") -> None:
        if policy_class not in _POLICIES:
            raise ValueError(f"unknown RLS policy class {policy_class!r}")
        self.table = table
        self.policy_class = policy_class
        self.model_label = model_label
        forward = _ENABLE.format(t=table) + "\n" + _POLICIES[policy_class].format(t=table)
        reverse = _DROP_POLICY[policy_class].format(t=table) + "\n" + _DISABLE.format(t=table)
        super().__init__(sql=forward, reverse_sql=reverse, elidable=False)

    def database_forwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return  # RLS is Postgres-only; no-op on the SQLite test DB
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(
        self, app_label: str, schema_editor: Any, from_state: ProjectState, to_state: ProjectState
    ) -> None:
        if schema_editor.connection.vendor != "postgresql":
            return
        super().database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self) -> str:
        return f"Enable+force RLS (class {self.policy_class}) on {self.table}"


# Forward: add app_is_platform() + recreate the Class T tenant_isolation policy
# with the platform-read branch. Reverse: restore the strict workspace-only policy
# and drop the accessor. Used by the alter migration that retrofits already-RLS'd
# Class T tables (backend-architecture §4.2).
_POLICY_T_STRICT = """
CREATE POLICY tenant_isolation ON "{t}" FOR ALL
    USING (workspace_id = app_workspace_id())
    WITH CHECK (workspace_id = app_workspace_id());
"""


class AddPlatformReadToClassT(migrations.RunSQL):
    """Retrofit the platform-read branch onto an already-installed Class T policy.

    Creates the ``app_is_platform()`` accessor (idempotent) and recreates the
    ``tenant_isolation`` policy on ``table`` so its USING clause admits the platform
    data plane (``app_is_platform()``) in addition to the row's own workspace. WITH
    CHECK is unchanged (strictly workspace-scoped). No-op off Postgres.
    """

    def __init__(self, *, table: str) -> None:
        self.table = table
        drop = f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";\n'
        forward = _CREATE_PLATFORM_ACCESSOR + "\n" + drop + _POLICY_T.format(t=table)
        reverse = drop + _POLICY_T_STRICT.format(t=table)
        super().__init__(sql=forward, reverse_sql=reverse, elidable=False)

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

    def describe(self) -> str:
        return f"Add platform-read branch to Class T policy on {self.table}"
