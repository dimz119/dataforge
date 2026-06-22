"""Internal Kafka publisher — the data-plane producer (backend-architecture §8.3
step 8, §8.6; delivery-channels §1; event-model §9).

The shard worker's last pipeline step publishes the post-ledger canonical-S-2
**internal** envelopes (all 20 fields + ``_df``) to ``df.delivery.events.v1``,
**keyed by ``partition_key``** (the workspace-prefixed S-5 key — same-key events
land on one partition, preserving per-entity order). The sink consumers (§8.6)
read this topic, ``strip_internal()`` at ingest, and deliver the 20-field shape.

This topic is the FINAL internal hop — it is *never* user-reachable (delivery
-channels §1: the consumption boundary is the REST cursor pull). The producer is
the only data-plane Kafka write (ADR-0006); its consistency story is the
idempotent ledger upstream + the at-least-once delivery contract downstream
(event-model §6), not Kafka transactions.

Fencing (§8.2 Kafka row): a publish cannot be transactionally fenced, so it is
**bounded** instead — at most one in-flight tick batch survives a lease loss. The
supervisor cancels a zombie worker between pipeline steps (heartbeat failure),
and :meth:`publish` flushes synchronously before returning, so a worker that lost
its lease can have published at most the single tick batch it was already inside.
Any resulting duplicate is an explicitly-licensed at-least-once duplicate on the
delivered stream (event-model §6); canonical truth (the ledger) is unaffected.

Pure host code: the engine never imports ``confluent_kafka`` (import-linter
contract 2). The producer is wrapped behind a small :class:`Producer` protocol so
unit tests inject a fake and assert the serialized bytes + key without a broker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import structlog

from dataforge_engine.envelope import canonical_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from dataforge_engine.envelope import InternalEnvelope

logger = structlog.get_logger("dataforge.runner.publisher")

__all__ = ["DELIVERY_TOPIC", "EventPublisher", "KafkaProducer"]

# §8.6 / event-model §9.1: the single internal delivery topic (12 partitions in
# compose). The runner is the sole producer; the sink consumer groups read it.
DELIVERY_TOPIC = "df.delivery.events.v1"


class KafkaProducer(Protocol):
    """The slice of ``confluent_kafka.Producer`` the publisher uses.

    A structural protocol so tests inject a fake producer (recording ``produce``
    calls + a ``flush`` count) without a broker, and the real adapter satisfies it
    by shape. ``produce`` is fire-and-async-buffered; ``flush`` blocks until the
    buffer drains (the bounded-in-flight guarantee, §8.2 Kafka row).
    """

    def produce(
        self,
        topic: str,
        *,
        key: bytes,
        value: bytes,
        on_delivery: Callable[[object, object], None] | None = ...,
    ) -> None: ...

    def flush(self, timeout: float = ...) -> int: ...

    def poll(self, timeout: float = ...) -> int: ...


def build_kafka_producer(bootstrap_servers: str, *, client_id: str) -> KafkaProducer:
    """Construct the real ``confluent_kafka.Producer`` (§8.2 producer config).

    Imported lazily so the module stays importable (and unit-testable with a fake)
    on a machine without ``librdkafka``; the runner host calls this at boot.
    ``enable.idempotence`` + ``acks=all`` give in-broker dedupe and durability so
    the bounded-in-flight rule's only residual is cross-takeover duplicates.
    """
    from confluent_kafka import Producer

    producer = Producer(
        {
            "bootstrap.servers": bootstrap_servers,
            "client.id": client_id,
            "acks": "all",
            "enable.idempotence": True,
            "compression.type": "lz4",
            "linger.ms": 5,
        }
    )
    return producer  # satisfies KafkaProducer structurally (by shape)


class EventPublisher:
    """Publishes canonical internal envelopes to ``df.delivery.events.v1`` (§8.3).

    One per shard worker. :meth:`publish` serializes each envelope with the S-2
    canonical serializer (byte-stable, ``Decimal``-aware) and produces it keyed by
    its ``partition_key`` (UTF-8 bytes). It flushes synchronously per tick batch so
    the §8.2 bounded-in-flight guarantee holds: a worker cancelled after a lost
    lease has published at most the one batch it was inside.
    """

    def __init__(
        self,
        producer: KafkaProducer,
        *,
        topic: str = DELIVERY_TOPIC,
        flush_timeout_s: float = 5.0,
    ) -> None:
        self._producer = producer
        self._topic = topic
        self._flush_timeout_s = flush_timeout_s
        self.published_total = 0

    def publish(self, envelopes: Sequence[InternalEnvelope]) -> int:
        """Produce a tick batch keyed by ``partition_key``; flush before returning.

        Returns the number of envelopes produced. An empty batch is a no-op (a
        starved or stopped tick). The synchronous flush bounds in-flight work to
        this single batch (§8.2 Kafka enforcement row).
        """
        if not envelopes:
            return 0
        import time

        from observation.infra import metrics

        # df_kafka_publish_duration_seconds (M-5 inner-loop): produce+flush wall time
        # for the tick batch; df_kafka_publish_total{result} counts per-envelope
        # produce outcomes (§4 kafka family). A flush failure is the producer's only
        # observable error here (produce is async-buffered).
        started = time.monotonic()
        for env in envelopes:
            key = str(env["partition_key"]).encode("utf-8")
            value = canonical_serialize(env)  # S-2 canonical bytes (internal shape)
            self._producer.produce(self._topic, key=key, value=value)
        # Bounded in-flight: drain this batch before the tick returns so a
        # subsequently-cancelled worker cannot leave a second batch outstanding.
        unflushed = self._producer.flush(self._flush_timeout_s)
        metrics.kafka_publish_duration_seconds.observe(time.monotonic() - started)
        produced = len(envelopes)
        if unflushed:
            # Some records did not drain within the flush budget — at-least-once
            # licenses the redelivery (§8.2); count the residual as failed/retried.
            failed = min(unflushed, produced)
            metrics.kafka_publish_total.labels(result="ok").inc(produced - failed)
            metrics.kafka_publish_total.labels(result="error").inc(failed)
            metrics.kafka_publish_retries_total.inc(failed)
            logger.warning(
                "publisher.flush_incomplete",
                topic=self._topic,
                unflushed=unflushed,
            )
        else:
            metrics.kafka_publish_total.labels(result="ok").inc(produced)
        self.published_total += produced
        return produced
