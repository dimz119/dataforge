"""``manage.py provision_db_roles`` — the two-role Postgres provisioner.

Idempotently creates the runtime application role ``dataforge_app`` and grants it
the least-privilege set from the role matrix (database-schema §11 / §9.2,
security-architecture SEC-TEN-2):

* ``dataforge_app`` is ``NOSUPERUSER NOBYPASSRLS`` — it is **subject to RLS**.
  Connecting the runtime as this role (not the owner/superuser) is what makes the
  Postgres RLS backstop actually bite (Phase 2 exit criterion #2/#6). A superuser
  or ``BYPASSRLS`` role silently ignores RLS even with ``FORCE`` set.
* It gets ``SELECT/INSERT/UPDATE/DELETE`` on every table **except** the
  append-only ``audit_log`` and the platform-managed ``workspace_quotas``, where
  it gets ``SELECT, INSERT`` only — encoding INV-AUD-1 and the §3.7 quota policy
  at the grant layer, not just in RLS/app code.
* ``ALTER DEFAULT PRIVILEGES`` so tables created by later migrations are covered
  without re-running grants (later phases add data-plane tables).
* ``CREATEDB`` so the test lane (pytest-django) can create+own the test database
  while still being a NOBYPASSRLS role — ``FORCE ROW LEVEL SECURITY`` then makes
  the policies apply to it as the test-table owner, so the §7.3 RLS probes run
  under a role RLS actually constrains.

Must run as the table owner (``dataforge_migrate`` / the bootstrap superuser),
which is what ``MIGRATE_DATABASE_URL`` carries. Safe to run repeatedly; the dev
``api`` entrypoint runs it right after ``migrate`` so the grants track the schema.
On non-Postgres backends it is a no-op (the SQLite unit lane has no roles).
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Role names are operator/settings controlled; restrict to a conservative
# identifier shape so the inlined DDL can never carry an injection payload.
_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(identifier: str) -> str:
    """Postgres-quote an SQL identifier (double quotes; internal quotes doubled)."""
    if not _ROLE_RE.match(identifier):
        raise CommandError(f"invalid role identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Postgres-quote a string literal (single quotes; internal quotes doubled)."""
    return "'" + value.replace("'", "''") + "'"

# Tables the runtime role may read+insert but never update/delete.
#   audit_log       — append-only (INV-AUD-1 / security §10.4)
#   workspace_quotas — created with the workspace; plan changes are platform-only
#                      until Phase 11 billing (database-schema §3.7 / §9.2)
_INSERT_ONLY_TABLES = ("audit_log", "workspace_quotas")


class Command(BaseCommand):
    help = (
        "Provision the dataforge_app runtime role (NOBYPASSRLS) and its "
        "least-privilege grants so the Postgres RLS backstop is enforced "
        "(SEC-TEN-2 / database-schema §11)."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--app-role",
            default=getattr(settings, "DB_APP_ROLE", "dataforge_app"),
            help="Runtime role name (default: dataforge_app).",
        )
        parser.add_argument(
            "--app-password",
            default=getattr(settings, "DB_APP_PASSWORD", "dataforge_app"),
            help="Runtime role password (dev default; prod sets via secret).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if connection.vendor != "postgresql":
            self.stdout.write("provision_db_roles: non-Postgres backend — no-op.")
            return

        role: str = options["app_role"]
        password: str = options["app_password"]
        db_name = connection.settings_dict["NAME"]

        # `role` / `password` are operator-controlled settings, not request input.
        # CREATE/ALTER ROLE cannot run as a parameterized statement, so we validate
        # the identifier (``_quote_ident``) and quote the password literal, then
        # issue CREATE or ALTER directly after a Python-side existence check —
        # avoiding any DO/EXECUTE quote-nesting (CREATE ROLE has no IF NOT EXISTS).
        role_q = _quote_ident(role)
        pw_lit = _quote_literal(password)
        attrs = "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB"

        with connection.cursor() as cursor:
            # 1. Create or update the role idempotently.
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            verb = "ALTER" if cursor.fetchone() else "CREATE"
            cursor.execute(f"{verb} ROLE {role_q} WITH {attrs} PASSWORD {pw_lit}")

            # 2. Connect + schema usage.
            self._exec(cursor, f'GRANT CONNECT ON DATABASE "{db_name}" TO "{role}";')
            self._exec(cursor, f'GRANT USAGE ON SCHEMA public TO "{role}";')

            # 3. Broad DML on existing + future tables, then tighten the
            #    append-only / platform-managed tables.
            self._exec(
                cursor,
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}";',
            )
            self._exec(
                cursor,
                f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}";',
            )
            self._exec(
                cursor,
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{role}";',
            )
            self._exec(
                cursor,
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                f'GRANT USAGE, SELECT ON SEQUENCES TO "{role}";',
            )

            # 4. Append-only / platform-managed tables: SELECT, INSERT only.
            for table in _INSERT_ONLY_TABLES:
                if self._table_exists(cursor, table):
                    self._exec(cursor, f'REVOKE UPDATE, DELETE ON "{table}" FROM "{role}";')

            # 5. EXECUTE on the GUC accessor functions the RLS policies call.
            self._exec(cursor, f'GRANT EXECUTE ON FUNCTION app_workspace_id() TO "{role}";')
            self._exec(cursor, f'GRANT EXECUTE ON FUNCTION app_user_id() TO "{role}";')

        self.stdout.write(
            self.style.SUCCESS(
                f"provision_db_roles: '{role}' (NOBYPASSRLS) provisioned on '{db_name}' "
                f"— append-only grants on {', '.join(_INSERT_ONLY_TABLES)}."
            )
        )

    @staticmethod
    def _exec(cursor: Any, sql: str) -> None:
        cursor.execute(sql)

    @staticmethod
    def _table_exists(cursor: Any, table: str) -> bool:
        cursor.execute("SELECT to_regclass(%s)", [f"public.{table}"])
        return cursor.fetchone()[0] is not None
