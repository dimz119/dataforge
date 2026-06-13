"""Postgres-backed test settings — the RLS-bearing lane (testing-strategy §7.3).

The default ``config.settings.test`` runs on in-memory SQLite so the unit lane is
fast and hermetic; RLS is a Postgres construct, so the raw-SQL RLS probes (§7.3)
and any other Postgres-only assertion **skip** there. This module is what the
compose / CI integration lane and the Phase 2 demo's RLS step run under: it keeps
every test-compression value from ``test`` (eager Celery, locmem email, fast
hashers, compressed lease TTLs) but restores the **Postgres** ``DATABASES`` from
``base`` so ``connection.vendor == "postgresql"`` and the RLS policies are live.

Run it via ``DJANGO_SETTINGS_MODULE=config.settings.test_postgres`` against a
reachable Postgres (the compose ``postgres`` service / a CI Postgres container):

    DJANGO_SETTINGS_MODULE=config.settings.test_postgres \
        uv run pytest tests/tenancy/test_rls_raw_sql.py

``FORCE ROW LEVEL SECURITY`` (tenancy.infra.rls) makes the policies apply even to
the migration/owner role, so the probes are meaningful regardless of the test DB
role.
"""

from config.settings.base import DATABASES as _BASE_DATABASES
from config.settings.test import *  # noqa: F403

# Restore the Postgres database from base (test.py overrode it to SQLite).
DATABASES = {"default": dict(_BASE_DATABASES["default"])}
# Per-test transactions: pytest-django's ``db`` fixture wraps each test so the
# transaction-local ``SET LOCAL app.workspace_id`` GUCs die with the test, exactly
# as ATOMIC_REQUESTS does per request in production (security §4.1, §9.4).
DATABASES["default"]["ATOMIC_REQUESTS"] = True
