"""Checkpoint + pool-snapshot persistence tests (database-schema §5.3-5.4; §9.1).

A finalized batch writes one ``stream_checkpoints`` row (the codec blob,
compressed) and one ``entity_pool_snapshots`` row per seeded entity type (the
commit-marker rule). These verify the rows land, decompress, and round-trip.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from generation.application import engine_driver
from generation.application.services import _plan_for
from generation.domain.models import EntityPoolSnapshot, StreamCheckpoint
from generation.infra.compression import decompress
from tests.generation.conftest import WorkspaceFixture


def _run(gen_workspace: WorkspaceFixture) -> str:
    stream_id = str(uuid.uuid4())
    plan = _plan_for(
        instance=gen_workspace.instance,
        workspace_id=str(gen_workspace.workspace.id),
        stream_id=stream_id,
        seed=42,
        simulated_days=1,
        virtual_epoch=datetime(2026, 1, 1, tzinfo=UTC),
    )
    engine_driver.run_batch(plan)
    return stream_id


@pytest.mark.django_db
def test_checkpoint_row_persisted_and_decodes(gen_workspace: WorkspaceFixture) -> None:
    """A finalized batch writes a decodable §9.1 checkpoint blob (compressed)."""
    stream_id = _run(gen_workspace)
    row = StreamCheckpoint.all_objects.get(stream_id=stream_id, shard_id=0)
    assert str(row.workspace_id) == str(gen_workspace.workspace.id)
    assert row.checkpoint_seq == 1
    assert row.last_sequence_no > 0
    blob: dict[str, Any] = json.loads(decompress(bytes(row.state)).decode("utf-8"))
    assert blob["codec_version"] == 1
    assert blob["sequence_no_last"] == row.last_sequence_no
    assert blob["pin_echo"]["seed"] == 42


@pytest.mark.django_db
def test_pool_snapshots_persisted_per_type(gen_workspace: WorkspaceFixture) -> None:
    """One snapshot row per seeded entity type, stamped with the checkpoint epoch."""
    stream_id = _run(gen_workspace)
    rows = {
        r.entity_type: r
        for r in EntityPoolSnapshot.all_objects.filter(stream_id=stream_id, shard_id=0)
    }
    # The seeded catalogs (users/products) must have a snapshot. The image is the
    # full post-generation pool (seeded + any created during the run), so the count
    # is at least the seeded size (exact seeding size is asserted at seed() time in
    # test_batch.test_pool_seeding_sizes_match_catalogs).
    assert "users" in rows and "products" in rows
    assert rows["users"].entity_count >= 100
    assert rows["products"].entity_count >= 50
    for row in rows.values():
        assert row.snapshot_epoch == 1
        lines = decompress(bytes(row.payload)).decode("utf-8").splitlines()
        assert len(lines) == row.entity_count
        first = json.loads(lines[0])
        assert "entity_key" in first and "entity_version" in first
