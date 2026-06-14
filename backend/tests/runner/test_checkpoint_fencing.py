"""Fenced checkpoint conditional-write tests (backend-architecture §8.2).

The runtime checkpoint is the §8.2 enforcement point: a guarded
``UPDATE … WHERE fencing_token <= mine``. A zombie (stale token) matches no row
and must raise :class:`runner.fencing.FencingError` — state can never roll back
(INV-STR-2). A fresh (greater-or-equal) token writes through. These exercise
:meth:`runner.checkpoint_store.CheckpointStore._upsert_fenced` against a real
``stream_checkpoints`` row, isolating the fencing rule from the engine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from runner.checkpoint_store import CheckpointStore
from runner.fencing import FencingError

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(db: Any) -> Any:
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    admin = User.objects.create_user(email="ckpt-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    ws = tenancy_services.create_workspace(user=admin, name="Ckpt Lab", slug=None)
    ws_context.activate(ws.id)
    return ws


def _seed_checkpoint(*, workspace_id: str, stream_id: str, fencing_token: int) -> None:
    """Insert a baseline checkpoint row at ``fencing_token`` (the takeover state)."""
    from generation.domain.models import StreamCheckpoint

    StreamCheckpoint.all_objects.create(
        workspace_id=workspace_id,
        stream_id=stream_id,
        shard_id=0,
        checkpoint_seq=1,
        fencing_token=fencing_token,
        state=b"\x00",
        state_format=1,
        last_sequence_no=10,
        virtual_clock_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _store(workspace_id: str, stream_id: str) -> CheckpointStore:
    return CheckpointStore(workspace_id=workspace_id, stream_id=stream_id, shard_id=0)


def test_stale_token_checkpoint_raises_fencing_error(workspace: Any) -> None:
    """A token below the stored one matches no row → FencingError (zombie fenced)."""
    ws_id = str(workspace.id)
    stream_id = str(uuid.uuid4())
    _seed_checkpoint(workspace_id=ws_id, stream_id=stream_id, fencing_token=10)
    store = _store(ws_id, stream_id)

    with pytest.raises(FencingError) as exc:
        store._upsert_fenced(
            fencing_token=5,  # stale: a newer holder (token 10) already took over
            checkpoint_seq=2,
            state=b"\x01",
            last_sequence_no=20,
            virtual_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    assert exc.value.surface == "checkpoint"
    assert exc.value.my_token == 5


def test_fresh_token_checkpoint_writes_through(workspace: Any) -> None:
    """A token >= the stored one writes through (the live holder advances state)."""
    from generation.domain.models import StreamCheckpoint

    ws_id = str(workspace.id)
    stream_id = str(uuid.uuid4())
    _seed_checkpoint(workspace_id=ws_id, stream_id=stream_id, fencing_token=10)
    store = _store(ws_id, stream_id)

    store._upsert_fenced(
        fencing_token=15,  # fresh: the new holder's token
        checkpoint_seq=2,
        state=b"\x02",
        last_sequence_no=30,
        virtual_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    row = StreamCheckpoint.all_objects.get(stream_id=stream_id, shard_id=0)
    assert row.fencing_token == 15
    assert row.checkpoint_seq == 2
    assert row.last_sequence_no == 30


def test_first_checkpoint_inserts_when_no_row(workspace: Any) -> None:
    """No existing row → the guarded INSERT lands (first start, not fenced)."""
    from generation.domain.models import StreamCheckpoint

    ws_id = str(workspace.id)
    stream_id = str(uuid.uuid4())
    store = _store(ws_id, stream_id)

    store._upsert_fenced(
        fencing_token=1,
        checkpoint_seq=1,
        state=b"\x03",
        last_sequence_no=5,
        virtual_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    row = StreamCheckpoint.all_objects.get(stream_id=stream_id, shard_id=0)
    assert row.fencing_token == 1
    assert row.last_sequence_no == 5
