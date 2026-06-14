"""Fixtures for the delivery-plane tests (delivery-channels §3-§4).

The buffer-writer writes to ``event_buffer`` through the Django ``default``
connection (the ``dataforge_app`` NOBYPASSRLS role at runtime); the caller arms the
per-batch workspace context (Layer-1 contextvar + Layer-2 ``app.workspace_id`` GUC)
before delivery so the rows pass RLS (SINK-7). These fixtures arm the same context
the production entrypoint (``runner.sinks.run._arm_tenant``) arms, keyed to the
shared engine test workspace (``dataforge_engine.envelope.tests.fixtures``).

The SQLite unit lane (default ``config.settings.test``) has no RLS — the migration
falls back to a plain ``event_buffer`` table — so these tests run hermetically with
no broker, no Postgres, and no real workspace row (``event_buffer`` carries no FK,
C-7). The Postgres-backed RLS / COPY assertions live under
``tests/delivery/test_postgres`` for the verify agent's lanes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from dataforge_engine.envelope.tests.fixtures import STREAM_ID, WORKSPACE_ID


@pytest.fixture
def armed_workspace(db: Any) -> Iterator[str]:
    """Arm the shared engine-fixture workspace for direct buffer-writer calls.

    Mirrors how ``runner.sinks.run._arm_tenant`` arms each batch's workspace before
    ``deliver``: the scoped-manager contextvar + the ``app.workspace_id`` GUC, both
    inside one transaction (the GUC is ``SET LOCAL``). Yields the workspace id so a
    test can assert on it; cleared on exit.
    """
    import uuid

    from tenancy.application.services import worker_workspace_scope

    with worker_workspace_scope(uuid.UUID(WORKSPACE_ID)):
        yield WORKSPACE_ID


@pytest.fixture
def stream_id() -> str:
    """The shared engine-fixture stream id (one stream, one writer, BW-7)."""
    return STREAM_ID
