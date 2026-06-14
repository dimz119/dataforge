"""``partition_key`` derivation tests (event-model §2.2.3, PK-1..3)."""

from __future__ import annotations

import pytest

from dataforge_engine.envelope import derive_partition_key
from dataforge_engine.envelope.partition import PartitionKeyError

from .fixtures import STREAM_ID, WORKSPACE_ID


def test_pk1_business_event_actor_root() -> None:
    """PK-1: business event keyed on the actor's root entity (event-model §7.1)."""
    key = derive_partition_key(
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        partition_entity_type="users",
        partition_entity_key="usr_a3f81c2e9b4d",
    )
    assert key == (
        "0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60:"
        "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b:users:usr_a3f81c2e9b4d"
    )


def test_pk2_cdc_event_mutated_entity() -> None:
    """PK-2: CDC event keyed on the mutated entity itself."""
    key = derive_partition_key(
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        partition_entity_type="orders",
        partition_entity_key="ord_5f2e7d1a8c3b",
    )
    assert key.endswith(":orders:ord_5f2e7d1a8c3b")


def test_workspace_id_is_mandatory_first_segment() -> None:
    """ADR-0002: workspace_id is the mandatory leading segment."""
    key = derive_partition_key(
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        partition_entity_type="users",
        partition_entity_key="usr_x",
    )
    assert key.split(":")[0] == WORKSPACE_ID


def test_component_with_colon_rejected() -> None:
    with pytest.raises(PartitionKeyError):
        derive_partition_key(
            workspace_id=WORKSPACE_ID,
            stream_id=STREAM_ID,
            partition_entity_type="users",
            partition_entity_key="usr:bad",
        )


def test_empty_component_rejected() -> None:
    with pytest.raises(PartitionKeyError):
        derive_partition_key(
            workspace_id=WORKSPACE_ID,
            stream_id=STREAM_ID,
            partition_entity_type="",
            partition_entity_key="usr_x",
        )


def test_over_long_key_rejected() -> None:
    with pytest.raises(PartitionKeyError):
        derive_partition_key(
            workspace_id=WORKSPACE_ID,
            stream_id=STREAM_ID,
            partition_entity_type="users",
            partition_entity_key="x" * 256,
        )
