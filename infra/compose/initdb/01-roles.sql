-- DataForge dev Postgres bootstrap — the two-role split (database-schema §11,
-- security-architecture SEC-TEN-2). Runs once, on first init of an empty pgdata
-- volume (docker-entrypoint-initdb.d).
--
-- The compose `dataforge` superuser stays the bootstrap owner / migrate role
-- (it owns the tables Django creates, i.e. acts as `dataforge_migrate`). This
-- script adds the *runtime* role `dataforge_app`:
--
--   * NOSUPERUSER + NOBYPASSRLS  -> RLS actually constrains it. Connecting the
--     api/ws/worker/runner/buffer-writer as this role (not the superuser) is what
--     makes the Postgres RLS backstop real (Phase 2 exit criterion #2/#6). A
--     superuser/BYPASSRLS connection silently ignores RLS even with FORCE set.
--
-- Table-level grants are (re)applied idempotently after every migrate by
-- `manage.py provision_db_roles` (run from the api entrypoint), which also tightens
-- audit_log / workspace_quotas to SELECT,INSERT only. This script grants the role
-- existence + connect + default privileges so the role can connect and read tables
-- the moment they are created, before that command runs.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataforge_app') THEN
        CREATE ROLE dataforge_app LOGIN PASSWORD 'dataforge_app'
            NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE dataforge TO dataforge_app;
GRANT USAGE ON SCHEMA public TO dataforge_app;

-- Existing + future tables/sequences (the migrate role owns them; these grants let
-- the app role use them). provision_db_roles re-runs these after each migrate and
-- removes UPDATE/DELETE on the append-only tables.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dataforge_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dataforge_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dataforge_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO dataforge_app;
