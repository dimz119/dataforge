"""Kafka consumer seam for the sink host (backend-architecture §8.6; delivery
-channels §3.5).

The sink host is a generic, channel-agnostic Kafka consumer harness; channels plug
into it (§3.5). It runs as a long-lived supervised data-plane process inside the
``runner`` group (never a Celery task, ADR-0006), with ``enable.auto.commit=false``
— offsets are committed **manually, only up to ``acked_through``** (SINK-3), and for
the buffer-writer that means *after* the DB transaction commits (BW-3,
at-least-once INV-DEL-3).

This module owns only the *seam*: a :class:`KafkaConsumer` structural protocol (the
slice of ``confluent_kafka.Consumer`` the host uses) and :func:`build_kafka_consumer`
(the real adapter, imported lazily so the module stays importable + unit-testable
without ``librdkafka``). The poll/batch/deliver/commit loop is the host
(``runner.sinks.host``).

Pure host code: the engine never imports ``confluent_kafka`` (import-linter
contract 2); the protocol lets unit tests inject a fake consumer and assert the
commit-after-insert ordering without a broker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = structlog.get_logger("dataforge.runner.sinks.consumer")

__all__ = [
    "REST_BUFFER_GROUP",
    "WEBSOCKET_GROUP",
    "ConsumerMessage",
    "KafkaConsumer",
    "TopicPartitionOffset",
    "build_kafka_consumer",
]

# The platform-wide buffer-writer consumer group (delivery-channels §4.1 BW-1;
# naming owned by backend-architecture §8.6). One group over the single delivery
# topic; MVP = 1 instance, Phase 11 scales by internal partition assignment.
REST_BUFFER_GROUP = "df.sink.rest-buffer.v1"

# The platform-wide ws-pusher consumer group (delivery-channels §6.1; naming owned
# by backend-architecture §8.6). One group over the single delivery topic; fans
# stripped events to the Redis channel-layer group ``stream_{stream_id}`` with a
# per-stream monotonic ``frame_seq``. A separate group from the buffer-writer so the
# two sinks consume the topic independently (one binding's lag never stalls the
# other, §3.5 isolation).
WEBSOCKET_GROUP = "df.sink.websocket.v1"


@runtime_checkable
class ConsumerMessage(Protocol):
    """The slice of ``confluent_kafka.Message`` the host reads.

    ``error()`` returns a truthy ``KafkaError`` on a non-record message (EOF,
    rebalance signal); ``value()`` is the serialized internal envelope bytes;
    ``key()`` is the ``partition_key`` bytes; ``topic``/``partition``/``offset``
    locate the record on the internal topic.
    """

    def error(self) -> object | None: ...
    def value(self) -> bytes | None: ...
    def key(self) -> bytes | None: ...
    def topic(self) -> str: ...
    def partition(self) -> int: ...
    def offset(self) -> int: ...


class TopicPartitionOffset:
    """A ``(topic, partition, offset)`` commit position (the host's offset cursor).

    The host commits ``offset = acked_through + 1`` (Kafka commits the *next* offset
    to consume) only after the channel reports durability (SINK-3 / BW-3).
    """

    __slots__ = ("offset", "partition", "topic")

    def __init__(self, *, topic: str, partition: int, offset: int) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset

    def __repr__(self) -> str:
        return f"TPO({self.topic}[{self.partition}]@{self.offset})"


@runtime_checkable
class KafkaConsumer(Protocol):
    """The slice of ``confluent_kafka.Consumer`` the sink host uses.

    A structural protocol so tests inject a fake (a scripted message queue + a
    recorded commit log) without a broker, and the real adapter satisfies it by
    shape. ``poll`` returns one message or ``None`` (linger timeout); ``commit``
    stores offsets synchronously; ``pause``/``resume`` implement backpressure
    (SINK-8); ``close`` leaves the group cleanly.
    """

    def poll(self, timeout: float = ...) -> ConsumerMessage | None: ...

    def commit(
        self,
        *,
        offsets: Sequence[TopicPartitionOffset] | None = ...,
        asynchronous: bool = ...,
    ) -> object: ...

    def pause(self, partitions: Sequence[object]) -> None: ...

    def resume(self, partitions: Sequence[object]) -> None: ...

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        on_assign: Callable[..., None] | None = ...,
        on_revoke: Callable[..., None] | None = ...,
    ) -> None: ...

    def close(self) -> None: ...


def build_kafka_consumer(
    bootstrap_servers: str, *, group_id: str, client_id: str
) -> KafkaConsumer:
    """Construct the real ``confluent_kafka.Consumer`` (§3.5 offset/rebalance policy).

    Imported lazily so the module stays importable (and unit-testable with a fake)
    on a machine without ``librdkafka``. ``enable.auto.commit=false`` (the host
    commits only ``acked_through``, SINK-3), cooperative-sticky assignment (§3.5
    rebalance), and ``auto.offset.reset=earliest`` so a fresh group reads the
    24 h-retained backlog rather than skipping it.

    Returns a thin :class:`_ConfluentConsumerAdapter` implementing the narrow
    :class:`KafkaConsumer` protocol: it translates the host's
    :class:`TopicPartitionOffset` commit cursor to ``confluent_kafka.TopicPartition``
    so the host stays broker-type-free (SINK-10).
    """
    from confluent_kafka import Consumer

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers,
            "group.id": group_id,
            "client.id": client_id,
            "enable.auto.commit": False,
            "partition.assignment.strategy": "cooperative-sticky",
            "auto.offset.reset": "earliest",
        }
    )
    return _ConfluentConsumerAdapter(consumer)


class _ConfluentConsumerAdapter:
    """Adapts ``confluent_kafka.Consumer`` to the narrow :class:`KafkaConsumer`.

    Translates the host's :class:`TopicPartitionOffset` (committing
    ``acked_through + 1``) to ``confluent_kafka.TopicPartition`` and pauses/resumes
    by ``(topic, partition)`` tuples — keeping the host free of broker types.
    """

    def __init__(self, consumer: object) -> None:
        self._c = consumer

    def poll(self, timeout: float = 0.0) -> ConsumerMessage | None:
        return self._c.poll(timeout)  # type: ignore[attr-defined,no-any-return]

    def commit(
        self,
        *,
        offsets: Sequence[TopicPartitionOffset] | None = None,
        asynchronous: bool = True,
    ) -> object:
        from confluent_kafka import TopicPartition

        if offsets is None:
            return self._c.commit(asynchronous=asynchronous)  # type: ignore[attr-defined]
        tps = [TopicPartition(o.topic, o.partition, o.offset) for o in offsets]
        return self._c.commit(offsets=tps, asynchronous=asynchronous)  # type: ignore[attr-defined]

    def _to_tps(self, partitions: Sequence[object]) -> list[object]:
        from confluent_kafka import TopicPartition

        return [TopicPartition(tp[0], tp[1]) for tp in partitions]  # type: ignore[index]

    def pause(self, partitions: Sequence[object]) -> None:
        self._c.pause(self._to_tps(partitions))  # type: ignore[attr-defined]

    def resume(self, partitions: Sequence[object]) -> None:
        self._c.resume(self._to_tps(partitions))  # type: ignore[attr-defined]

    def subscribe(
        self,
        topics: Sequence[str],
        *,
        on_assign: Callable[..., None] | None = None,
        on_revoke: Callable[..., None] | None = None,
    ) -> None:
        kwargs: dict[str, Callable[..., None]] = {}
        if on_assign is not None:
            kwargs["on_assign"] = on_assign
        if on_revoke is not None:
            kwargs["on_revoke"] = on_revoke
        self._c.subscribe(list(topics), **kwargs)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._c.close()  # type: ignore[attr-defined]
