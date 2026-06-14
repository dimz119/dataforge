"""EventPublisher tests (backend-architecture §8.3 step 8, §8.6).

The publisher serializes post-ledger canonical-S-2 internal envelopes and produces
them to ``df.delivery.events.v1`` keyed by ``partition_key`` (workspace-prefixed
S-5), flushing synchronously per tick batch (the bounded-in-flight rule, §8.2).
These exercise the contract against a fake producer — no broker — asserting the
serialized bytes equal the canonical serializer's output, the key is the
``partition_key`` UTF-8 bytes, and an empty batch is a no-op.
"""

from __future__ import annotations

from typing import Any

from dataforge_engine.envelope import (
    canonical_serialize,
    make_canonical_df,
    make_schema_ref,
)
from runner.publisher import DELIVERY_TOPIC, EventPublisher


class FakeProducer:
    """Records ``produce`` calls and counts ``flush`` (the KafkaProducer protocol)."""

    def __init__(self, *, unflushed: int = 0) -> None:
        self.produced: list[tuple[str, bytes, bytes]] = []
        self.flush_calls = 0
        self._unflushed = unflushed

    def produce(self, topic: str, *, key: bytes, value: bytes, on_delivery: Any = None) -> None:
        self.produced.append((topic, key, value))

    def flush(self, timeout: float = 0.0) -> int:
        self.flush_calls += 1
        return self._unflushed

    def poll(self, timeout: float = 0.0) -> int:
        return 0


def _envelope(*, partition_key: str, sequence_no: int) -> dict[str, Any]:
    """A minimal valid 20-field internal envelope (+ canonical ``_df``)."""
    return {
        "envelope_version": "1.0",
        "event_id": "0190000000007b2a8000000000000001",
        "workspace_id": "ws-1",
        "stream_id": "stream-1",
        "shard_id": 0,
        "scenario_slug": "ecommerce",
        "manifest_version": "1.0.0",
        "event_type": "order_placed",
        "schema_ref": make_schema_ref("ecommerce", "order_placed", 1),
        "sequence_no": sequence_no,
        "partition_key": partition_key,
        "occurred_at": "2026-01-01T00:00:00.000Z",
        "emitted_at": "2026-01-01T00:00:00.001Z",
        "actor_id": "users:abc",
        "session_id": None,
        "entity_refs": [],
        "correlation_id": "corr-1",
        "causation_id": None,
        "op": None,
        "payload": {"total": 10},
        "_df": make_canonical_df(),
    }


def test_publish_keys_by_partition_key_and_serializes_canonically() -> None:
    producer = FakeProducer()
    publisher = EventPublisher(producer)
    env = _envelope(partition_key="ws-1:stream-1:users:abc", sequence_no=1)

    count = publisher.publish([env])  # type: ignore[list-item]

    assert count == 1
    assert len(producer.produced) == 1
    topic, key, value = producer.produced[0]
    assert topic == DELIVERY_TOPIC
    assert key == b"ws-1:stream-1:users:abc"  # S-5 workspace-prefixed key
    assert value == canonical_serialize(env)  # byte-identical canonical S-2
    assert producer.flush_calls == 1  # bounded in-flight: flush per batch
    assert publisher.published_total == 1


def test_publish_empty_batch_is_noop() -> None:
    producer = FakeProducer()
    publisher = EventPublisher(producer)

    assert publisher.publish([]) == 0
    assert producer.produced == []
    assert producer.flush_calls == 0  # no flush for an empty (starved/stopped) tick


def test_publish_batch_preserves_order_and_distinct_keys() -> None:
    producer = FakeProducer()
    publisher = EventPublisher(producer)
    batch = [
        _envelope(partition_key="ws-1:stream-1:users:a", sequence_no=1),
        _envelope(partition_key="ws-1:stream-1:users:b", sequence_no=2),
    ]

    count = publisher.publish(batch)  # type: ignore[arg-type]

    assert count == 2
    keys = [k for _t, k, _v in producer.produced]
    assert keys == [b"ws-1:stream-1:users:a", b"ws-1:stream-1:users:b"]
    assert producer.flush_calls == 1  # one flush bounds the whole tick batch
