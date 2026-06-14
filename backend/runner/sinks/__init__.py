"""Delivery sink consumers — the §8.6 Kafka consumer-group members (data plane).

Phase 5 replaces the Phase-1 stub with the real buffer-writer sink: the generic
sink host (:class:`~runner.sinks.host.SinkHost`, delivery-channels §3.5), the Kafka
consumer seam (:mod:`runner.sinks.consumer`), and the production wiring
(:func:`runner.sinks.run.build_buffer_writer_host`) that plugs the ``rest_buffer``
:class:`~delivery.infra.buffer_writer_channel.BufferWriterChannel` into it.

Sinks are Kafka consumer-group members, not leased shard owners (§8.6): the broker's
group coordinator does the placement/failover the Redis lease does for generation,
so no lease is held here. The runner supervisor starts the host in a worker thread
when ``--role`` includes ``sinks``.

WS-pusher (``df.sink.websocket.v1``) ships in Phase 6 against the same host.
"""

from __future__ import annotations

from runner.sinks.consumer import (
    REST_BUFFER_GROUP,
    KafkaConsumer,
    build_kafka_consumer,
)
from runner.sinks.host import SinkHost
from runner.sinks.run import build_buffer_writer_host, deserialize_internal

__all__ = [
    "REST_BUFFER_GROUP",
    "KafkaConsumer",
    "SinkHost",
    "build_buffer_writer_host",
    "build_kafka_consumer",
    "deserialize_internal",
]
