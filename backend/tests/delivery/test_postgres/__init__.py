"""Postgres-lane delivery tests (delivery-channels §4; database-schema §6.1, §9.5/§9.7).

Run under ``config.settings.test_postgres`` where RLS, hourly RANGE partitioning,
and the binary ``COPY`` path are live; they skip on the SQLite unit DB. The verify
agent owns the compose/CI Postgres runtime — these are isolated from the fast unit
lane so the RLS-sensitive (NOBYPASSRLS ``dataforge_app`` role) assertions run only
where they bite.
"""
