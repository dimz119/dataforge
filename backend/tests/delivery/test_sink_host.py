"""Sink host harness tests (delivery-channels §3.5; backend-architecture §8.6).

The generic :class:`~runner.sinks.host.SinkHost` against a *fake* Kafka consumer (a
scripted message queue + a recorded commit log) and a *fake* channel — no broker,
no DB. Proves the host-side contract the buffer-writer relies on:

* **commit-after-insert ordering (SINK-3 / BW-3 / INV-DEL-3).** The host commits the
  Kafka offset (``acked_through + 1``) only *after* the channel's ``deliver``
  returns ``ok`` — the call order is deliver → commit, never the reverse.
* **no commit on backpressure (SINK-8).** A ``backpressure`` result pauses the
  partition and commits nothing (the range redelivers on resume).
* **batching ≤ 500 / 250 ms (BW-2).** The accumulator caps a window at
  ``max_batch_events`` and groups by ``(topic, partition, stream)``.
* **fatal stops the binding (SINK-9).** A ``fatal`` result halts the host loop.

These are framework-light (the host imports only the channel contract + the consumer
seam); the fakes record the exact deliver/commit interleaving.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from dataforge_engine.envelope import canonical_serialize_str
from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
from delivery.domain.channel import DeliveryBatch, DeliveryResult, SinkError
from runner.sinks.consumer import TopicPartitionOffset
from runner.sinks.host import SinkHost

TOPIC = "df.delivery.events.v1"


class _FakeMessage:
    """A scripted ``ConsumerMessage`` (one record on the internal topic)."""

    def __init__(self, *, value: bytes, offset: int, partition: int = 0) -> None:
        self._value = value
        self._offset = offset
        self._partition = partition

    def error(self) -> None:
        return None

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes | None:
        return None

    def topic(self) -> str:
        return TOPIC

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class _FakeConsumer:
    """A scripted consumer: a finite message queue + a recorded commit log.

    ``poll`` drains the queue then returns ``None`` (linger timeout); ``commit``
    records the committed ``(topic, partition, offset)``; ``pause``/``resume`` record
    the partitions toggled. ``trace`` is the shared deliver/commit interleaving log.
    """

    def __init__(self, messages: list[_FakeMessage], trace: list[str]) -> None:
        self._queue: deque[_FakeMessage] = deque(messages)
        self.committed: list[TopicPartitionOffset] = []
        self.paused: list[tuple[str, int]] = []
        self.resumed: list[tuple[str, int]] = []
        self.subscribed: list[str] = []
        self._trace = trace

    def subscribe(self, topics: Any, **_: Any) -> None:
        self.subscribed = list(topics)

    def poll(self, timeout: float = 0.0) -> _FakeMessage | None:
        return self._queue.popleft() if self._queue else None

    def commit(
        self, *, offsets: Any = None, asynchronous: bool = True
    ) -> object:
        for off in offsets or []:
            self._trace.append(f"commit:{off.offset}")
            self.committed.append(off)
        return None

    def pause(self, partitions: Any) -> None:
        self.paused.extend(tuple(p) for p in partitions)

    def resume(self, partitions: Any) -> None:
        self.resumed.extend(tuple(p) for p in partitions)

    def high_watermark(self, topic: str, partition: int) -> int | None:
        # The unit-test fake does not expose broker watermarks (host.py treats a
        # ``None`` probe as "lag metric simply not updated this commit").
        return None

    def close(self) -> None:
        return None


class _RecordingChannel:
    """A channel that records each ``deliver`` and returns a scripted result."""

    def __init__(self, result: DeliveryResult, trace: list[str]) -> None:
        self._result = result
        self._trace = trace
        self.batches: list[DeliveryBatch] = []

    def deliver(self, batch: DeliveryBatch) -> DeliveryResult:
        self._trace.append(f"deliver:{batch.last_offset}")
        self.batches.append(batch)
        # ok results ack through the batch's last offset (the buffer-writer contract).
        if self._result.status == "ok":
            return DeliveryResult.ok(acked_through=batch.last_offset)
        return self._result

    def flush(self, reason: Any) -> DeliveryResult:
        return DeliveryResult.ok(acked_through=None)

    def close(self) -> None:
        return None

    def healthcheck(self) -> Any:
        return None


def _record_bytes(offset: int) -> bytes:
    """One internal record's canonical-JSON bytes (distinct event per offset)."""
    return canonical_serialize_str(order_placed_envelope(seed=4242 + offset)).encode()


def _messages(n: int) -> list[_FakeMessage]:
    return [_FakeMessage(value=_record_bytes(i), offset=i) for i in range(n)]


def _host(consumer: Any, channel: Any) -> SinkHost:
    return SinkHost(
        consumer,
        channel,
        topics=[TOPIC],
        deserialize=lambda raw: json.loads(raw.decode("utf-8")),
        linger_ms=5,
    )


def test_commit_follows_deliver_ok() -> None:
    """The host commits ``acked_through + 1`` only *after* a successful deliver."""
    trace: list[str] = []
    consumer = _FakeConsumer(_messages(3), trace)
    channel = _RecordingChannel(DeliveryResult.ok(acked_through=None), trace)
    host = _host(consumer, channel)

    delivered = host.run_once()

    assert delivered == 3
    # deliver precedes commit, and the committed offset is last_offset + 1 (=3).
    assert trace == ["deliver:2", "commit:3"]
    assert [o.offset for o in consumer.committed] == [3]


def test_no_commit_on_backpressure_and_partition_paused() -> None:
    """A ``backpressure`` result commits nothing and pauses the partition (SINK-8)."""
    trace: list[str] = []
    consumer = _FakeConsumer(_messages(2), trace)
    channel = _RecordingChannel(
        DeliveryResult.backpressure(retry_after_ms=500), trace
    )
    host = _host(consumer, channel)

    delivered = host.run_once()

    assert delivered == 0
    assert consumer.committed == [], "no offset committed on backpressure"
    assert (TOPIC, 0) in consumer.paused
    assert trace == ["deliver:1"]  # delivered once, never committed


def test_fatal_stops_the_host() -> None:
    """A ``fatal`` result stops the host loop for the binding (SINK-9)."""
    trace: list[str] = []
    consumer = _FakeConsumer(_messages(1), trace)
    channel = _RecordingChannel(
        DeliveryResult.fatal(
            SinkError(error_class="fatal_contract", message="bad envelope")
        ),
        trace,
    )
    host = _host(consumer, channel)

    host.run_once()
    assert host._stopped is True
    assert consumer.committed == []


def test_batch_caps_at_max_batch_events() -> None:
    """The accumulator caps one window at ``max_batch_events`` (BW-2 ≤ 500)."""
    trace: list[str] = []
    consumer = _FakeConsumer(_messages(10), trace)
    channel = _RecordingChannel(DeliveryResult.ok(acked_through=None), trace)
    host = SinkHost(
        consumer,
        channel,  # type: ignore[arg-type]
        topics=[TOPIC],
        deserialize=lambda raw: json.loads(raw.decode("utf-8")),
        linger_ms=50,
        max_batch_events=4,
    )

    delivered = host.run_once()
    assert delivered == 4  # one window stops at the cap, leaving 6 for the next window
    assert channel.batches[0].count == 4
    assert channel.batches[0].last_offset == 3
