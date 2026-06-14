# Retrofit the platform-read branch onto every Class T tenant_isolation policy
# (backend-architecture §4.2 / §8.3). The runner data plane reconciles EVERY
# workspace's shards (its claimable scan + per-shard desired/checkpoint reads cross
# tenants by design, INV-STR-6) and the flat single-resource API routes must resolve
# a resource's owning workspace BEFORE any workspace context is armed. Both are
# pre-context cross-tenant SELECTs the strict Class T policy hides from the
# NOBYPASSRLS runtime role. This migration creates app_is_platform() and recreates
# each Class T policy so its USING (read) clause also admits the platform GUC
# (app.platform = 'on'); WITH CHECK stays strictly workspace-scoped, so platform
# reads can never become cross-tenant writes.
#
# Depends on every app whose 0001 installs a Class T table so the policy exists to
# be recreated. The six Class T tables: streams, stream_shards (streams.0001),
# ground_truth_ledger, stream_checkpoints, entity_pool_snapshots, datasets
# (generation.0001), event_buffer (delivery.0001).
from __future__ import annotations

from django.db import migrations

from tenancy.infra.rls import AddPlatformReadToClassT

_CLASS_T_TABLES = (
    "streams",
    "stream_shards",
    "ground_truth_ledger",
    "stream_checkpoints",
    "entity_pool_snapshots",
    "datasets",
    "event_buffer",
)


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0002_api_key_auth_bootstrap_rls"),
        ("streams", "0001_initial"),
        ("generation", "0001_initial"),
        ("delivery", "0001_initial"),
    ]

    operations = [AddPlatformReadToClassT(table=t) for t in _CLASS_T_TABLES]
