"""Dependency probes for /readyz (observability §6.1).

Each probe performs exactly the check the spec names and returns nothing on
success; failures raise. Timeout and result caching are owned by
`observation.application.readiness`.
"""

import redis
from confluent_kafka.admin import AdminClient
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def probe_postgres() -> None:
    """`SELECT 1` against the default connection (observability §6.2)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


def probe_redis() -> None:
    """`PING` against REDIS_URL (observability §6.2)."""
    client = redis.Redis.from_url(
        settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        client.ping()
    finally:
        client.close()


def probe_kafka() -> None:
    """Broker metadata fetch against KAFKA_BOOTSTRAP_SERVERS (observability §6.2)."""
    admin = AdminClient({"bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS})
    admin.list_topics(timeout=2)


def probe_migrations() -> None:
    """No unapplied migrations on the default database (observability §6.2)."""
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        raise RuntimeError(f"{len(plan)} unapplied migration(s)")
