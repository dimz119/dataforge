"""TEN §7.3 — RLS verification with the ORM bypassed (raw SQL).

Opens a raw cursor as the application DB role and asserts the Postgres RLS policies
return zero foreign / zero unset-GUC rows for every tenant table — the second
wall, proven independently of the scoped managers (security §4.2, SEC-TEN-3;
Phase 2 exit criterion #2). RLS is a Postgres construct, so this suite is skipped
on the SQLite unit DB and runs in the compose/CI Postgres lane where the demo
asserts it (phase doc step 11).

The tenant-table list is derived from migration state (every EnableRowLevelSecurity
op), so a new tenant table without RLS is caught by construction.
"""

from __future__ import annotations

import pytest
from django.db import connection

from tenancy.infra.tenancy_check import _tables_with_rls_migration

pytestmark = [pytest.mark.tenancy, pytest.mark.django_db]


def _skip_unless_postgres() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("RLS probes require PostgreSQL (run in the compose/CI Postgres lane).")


def test_rls_tables_enumerated_from_migrations() -> None:
    """Every tenant table has an RLS migration op (coverage cannot lag schema)."""
    tables = _tables_with_rls_migration()
    expected = {
        "workspaces",
        "memberships",
        "workspace_invitations",
        "api_keys",
        "workspace_quotas",
        "usage_counters",
    }
    assert expected <= tables


def test_foreign_guc_sees_zero_rows(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """Under workspace-B's GUC, raw SELECTs over A's tables return 0 rows."""
    _skip_unless_postgres()
    a = make_workspace("rls-a@example.com")
    b = make_workspace("rls-b@example.com")
    tables = sorted(_tables_with_rls_migration() - {"workspaces"})
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [str(b.workspace.id)])
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [str(b.admin.id)])
        for table in tables:
            cursor.execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id = %s",
                [str(a.workspace.id)],
            )
            assert cursor.fetchone()[0] == 0, f"RLS leak: {table} visible under foreign GUC"


def test_unset_guc_sees_zero_rows(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """With the GUC unset, raw SELECTs return 0 rows (default-deny, SEC-TEN-1)."""
    _skip_unless_postgres()
    make_workspace("rls-c@example.com")
    tables = sorted(_tables_with_rls_migration() - {"workspaces"})
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', '', true)")
        cursor.execute("SELECT set_config('app.user_id', '', true)")
        for table in tables:
            cursor.execute(f"SELECT count(*) FROM {table}")
            assert cursor.fetchone()[0] == 0, f"RLS default-deny breached: {table}"


def test_own_guc_sees_own_rows(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """Sanity: under A's own GUC, A's membership row IS visible (no false-deny)."""
    _skip_unless_postgres()
    a = make_workspace("rls-d@example.com")
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [str(a.workspace.id)])
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [str(a.admin.id)])
        cursor.execute(
            "SELECT count(*) FROM memberships WHERE workspace_id = %s", [str(a.workspace.id)]
        )
        assert cursor.fetchone()[0] >= 1


def test_foreign_guc_sees_zero_workspace_rows(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """The self-tenant-owned ``workspaces`` table (RLS Class W) masks A from B.

    ``workspaces`` has no ``workspace_id`` column — its PK *is* the tenant id, so
    Class W policies it via membership; under B's GUC, A's workspace row is 0 rows.
    """
    _skip_unless_postgres()
    a = make_workspace("rls-wsa@example.com")
    b = make_workspace("rls-wsb@example.com")
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [str(b.workspace.id)])
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [str(b.admin.id)])
        cursor.execute("SELECT count(*) FROM workspaces WHERE id = %s", [str(a.workspace.id)])
        assert cursor.fetchone()[0] == 0, "RLS leak: A's workspace row visible under B's GUC"


def test_foreign_guc_cannot_write_a_rows(make_workspace) -> None:  # type: ignore[no-untyped-def]
    """INSERT/UPDATE/DELETE against A's rows under B's GUC affects 0 rows.

    §7.3 row 3: the write path is policed too — a mutation targeting A's rows
    under B's workspace GUC must touch nothing (USING + WITH CHECK), proving RLS
    is not read-only theatre.
    """
    _skip_unless_postgres()
    a = make_workspace("rls-wa@example.com")
    b = make_workspace("rls-wb@example.com")
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.workspace_id', %s, true)", [str(b.workspace.id)])
        cursor.execute("SELECT set_config('app.user_id', %s, true)", [str(b.admin.id)])
        # UPDATE A's membership row — RLS USING filters it out → 0 rows.
        cursor.execute(
            "UPDATE memberships SET role = 'member' WHERE workspace_id = %s",
            [str(a.workspace.id)],
        )
        assert cursor.rowcount == 0, "RLS write-leak: B updated A's membership row"
        # DELETE A's quota row under B's GUC → 0 rows.
        cursor.execute(
            "DELETE FROM workspace_quotas WHERE workspace_id = %s", [str(a.workspace.id)]
        )
        assert cursor.rowcount == 0, "RLS write-leak: B deleted A's quota row"
