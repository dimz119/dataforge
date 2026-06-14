"""Partition-maintenance task tests (backend-architecture §7.1 maintenance queue).

The maintenance beat tasks delegate to the partition managers. On the SQLite unit
lane partitioning is a no-op (a Postgres construct), so the tasks return empty
result sets without error — the contract this lane asserts. The real partition DDL
is exercised in the Postgres lane / compose demo.
"""

from __future__ import annotations

import pytest
from django.db import connection

from streams.infra import partition_maint
from streams.tasks import maintenance

_sqlite_only = pytest.mark.skipif(
    connection.vendor == "postgresql",
    reason="partition DDL is live on Postgres (real partitions created); this asserts "
    "the SQLite no-op contract — the Postgres path is covered by the compose demo.",
)


@_sqlite_only
@pytest.mark.django_db
def test_ledger_maint_noop_on_sqlite(db: object) -> None:
    result = maintenance.maintain_ledger_partitions()
    assert result == {"created": [], "dropped": []}


@_sqlite_only
@pytest.mark.django_db
def test_buffer_maint_noop_on_sqlite(db: object) -> None:
    result = maintenance.maintain_buffer_partitions()
    assert result == {"created": [], "dropped": []}


def test_buffer_partition_manager_seam_resolves() -> None:
    # The delivery buffer-partition seam (delivery.infra.partitions) is now shipped;
    # the maintenance loader resolves it and exposes the hourly DDL API the beat needs.
    mgr = partition_maint._load_buffer_partition_manager()
    assert mgr is not None
    assert hasattr(mgr, "ensure_partitions")
    assert hasattr(mgr, "drop_partition")
