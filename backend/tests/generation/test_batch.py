"""Generation batch end-to-end + ledger + quota tests (Phase 4, SQLite unit lane).

Covers the host-side proving rows of the phase contract that don't need RLS:

* a small sync batch end-to-end produces N events into the ledger;
* gapless ``sequence_no`` per (stream, shard) in the ledger (INV-GEN-7);
* pool seeding sizes match the merged manifest catalogs (§4.5);
* a backfill quota cap rejects an over-large request (PRD §7);
* the download artifact is delivered-shape — 20-key envelopes, ``_df`` stripped
  (INV-DEL-2).

The RLS-sensitive variants (cross-tenant masking, runtime-role writes) live under
``tests/generation/test_postgres`` for the verify agent's Postgres lane.
"""

from __future__ import annotations

import gzip
import json
import uuid
from typing import Any

import pytest

from dataforge_engine.envelope import DELIVERED_FIELD_SET
from generation.application import services
from generation.domain.models import (
    DATASET_READY,
    Dataset,
    GroundTruthLedger,
)
from tests.generation.conftest import WorkspaceFixture


def _create_sync(gen_workspace: WorkspaceFixture, **kwargs: Any) -> Dataset:
    params: dict[str, Any] = {
        "workspace": gen_workspace.workspace,
        "scenario_instance_id": gen_workspace.instance.id,
        "name": "test-batch",
        "seed": 42,
        "simulated_days": 1,
        "virtual_epoch": None,
        "compression": "gzip",
        "actor": gen_workspace.admin,
    }
    params.update(kwargs)
    result = services.create_dataset(**params)
    return result.dataset


@pytest.mark.django_db
def test_small_batch_produces_events(gen_workspace: WorkspaceFixture) -> None:
    """A small sync batch reaches ``ready`` with a positive event count → ledger."""
    dataset = _create_sync(gen_workspace)
    assert dataset.status == DATASET_READY
    assert dataset.event_count is not None and dataset.event_count > 0
    ledger_rows = GroundTruthLedger.all_objects.filter(stream_id=dataset.stream_id).count()
    assert ledger_rows == dataset.event_count


@pytest.mark.django_db
def test_ledger_sequence_is_gapless(gen_workspace: WorkspaceFixture) -> None:
    """``sequence_no`` is gapless 1..N per (stream, shard) in the ledger (INV-GEN-7)."""
    dataset = _create_sync(gen_workspace)
    seqs = list(
        GroundTruthLedger.all_objects.filter(stream_id=dataset.stream_id)
        .order_by("sequence_no")
        .values_list("sequence_no", flat=True)
    )
    assert seqs == list(range(1, len(seqs) + 1))


@pytest.mark.django_db
def test_ledger_rows_carry_workspace_and_canonical_df(
    gen_workspace: WorkspaceFixture,
) -> None:
    """Every ledger row is denormalized with workspace_id + ``_df.canonical = true``."""
    dataset = _create_sync(gen_workspace)
    rows = list(GroundTruthLedger.all_objects.filter(stream_id=dataset.stream_id)[:25])
    assert rows
    for row in rows:
        assert str(row.workspace_id) == str(gen_workspace.workspace.id)
        env = row.envelope if isinstance(row.envelope, dict) else json.loads(row.envelope)
        assert env["_df"]["canonical"] is True


@pytest.mark.django_db
def test_pool_seeding_sizes_match_catalogs(gen_workspace: WorkspaceFixture) -> None:
    """The seeded pools size to the merged catalog sizes (overlay: users 100 / 50)."""
    from generation.application import engine_driver
    from generation.application.services import _plan_for

    plan = _plan_for(
        instance=gen_workspace.instance,
        workspace_id=str(gen_workspace.workspace.id),
        stream_id=str(uuid.uuid4()),
        seed=42,
        simulated_days=1,
        virtual_epoch=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__(
            "datetime").UTC),
    )
    shard = engine_driver.build_shard(plan)
    shard.seed()
    assert shard.pools.count("users") == 100
    assert shard.pools.count("products") == 50


@pytest.mark.django_db
def test_quota_cap_rejects_oversized_request(gen_workspace: WorkspaceFixture) -> None:
    """A request over the Free 7-day backfill cap is rejected before any row (PRD §7)."""
    with pytest.raises(services.QuotaExceeded) as exc:
        services.create_dataset(
            workspace=gen_workspace.workspace,
            scenario_instance_id=gen_workspace.instance.id,
            name="too-big",
            seed=42,
            simulated_days=30,  # > Free's 7-day cap
            virtual_epoch=None,
            compression="gzip",
            actor=gen_workspace.admin,
        )
    assert exc.value.quota == "simulated_days"
    assert exc.value.limit == 7
    assert not Dataset.all_objects.filter(name="too-big").exists()


@pytest.mark.django_db
def test_download_artifact_is_delivered_shape(gen_workspace: WorkspaceFixture) -> None:
    """The gzipped JSONL carries delivered-shape envelopes — exactly 20 keys, no _df."""
    dataset = _create_sync(gen_workspace)
    assert dataset.file_path
    with gzip.open(dataset.file_path, "rt", encoding="utf-8") as fh:
        lines = [line for line in fh if line.strip()]
    assert len(lines) == dataset.event_count
    for raw in lines[:50]:
        obj = json.loads(raw)
        assert "_df" not in obj
        assert set(obj.keys()) == DELIVERED_FIELD_SET
        assert len(obj) == 20


@pytest.mark.django_db
def test_same_seed_reproduces_identical_ledger(gen_workspace: WorkspaceFixture) -> None:
    """Two batches at the same seed + pinned epoch yield identical event ids (INV-GEN-3).

    The canonical content is a pure function of (manifest, seed, merged config) and
    the simulated clock — never the random stream_id or wall pacing — so pinning
    ``virtual_epoch`` makes the two batches byte-identical in event id order.
    """
    from datetime import UTC, datetime

    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    first = _create_sync(gen_workspace, name="run-a", virtual_epoch=epoch)
    second = _create_sync(gen_workspace, name="run-b", virtual_epoch=epoch)
    ids_a = list(
        GroundTruthLedger.all_objects.filter(stream_id=first.stream_id)
        .order_by("sequence_no")
        .values_list("event_id", flat=True)
    )
    ids_b = list(
        GroundTruthLedger.all_objects.filter(stream_id=second.stream_id)
        .order_by("sequence_no")
        .values_list("event_id", flat=True)
    )
    assert ids_a and ids_a == ids_b
