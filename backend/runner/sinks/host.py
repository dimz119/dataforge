"""The generic sink host (delivery-channels §3.5; backend-architecture §8.6).

A channel-agnostic Kafka consumer harness: it polls the internal delivery topic in
batches of ≤ 500 events with a 250 ms linger (SINK-1 / BW-2), groups records by
``(stream_id, internal topic-partition)`` (single-writer-per-stream, BW-7), hands
each batch to the channel's :meth:`~delivery.domain.channel.DeliveryChannel.deliver`,
and **commits Kafka offsets only up to the returned ``acked_through``** (SINK-3) —
for the buffer-writer that is *after* the DB transaction commits (BW-3,
at-least-once INV-DEL-3). On ``backpressure`` it pauses the partition + backs off
(SINK-8); on ``fatal`` it stops consumption for the binding (SINK-9).

The host owns the consumer-group lifecycle, batching, offset policy, rebalance
flush, and backpressure clamp; the channel owns one batch's durability. The host
holds no Redis lease (§8.6: sinks are Kafka consumer-group members, not leased
shard owners — the broker's group coordinator does the work the lease does for
generation).

This is synchronous host code (the confluent_kafka consumer is a C client driven by
``poll``); the supervisor runs it in a worker thread (``runner.sinks.run``). It
imports the channel interface from :mod:`delivery.domain.channel` and the concrete
buffer-writer is wired in by the entrypoint, so the host never imports a concrete
channel (SINK-10 / app layering).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from delivery.domain.channel import (
    MAX_BATCH_EVENTS,
    DeliveryBatch,
    clamp_backpressure_ms,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from dataforge_engine.envelope import EnvelopeMapping
    from delivery.domain.channel import DeliveryChannel
    from runner.sinks.consumer import (
        ConsumerMessage,
        KafkaConsumer,
        TopicPartitionOffset,
    )

logger = structlog.get_logger("dataforge.runner.sinks.host")

__all__ = ["LINGER_MS", "POLL_TIMEOUT_S", "BatchKey", "SinkHost"]

# BW-2: ≤ 500 events / 250 ms linger. The poll timeout is the linger budget.
LINGER_MS = 250
POLL_TIMEOUT_S = 0.25


@dataclass(frozen=True)
class BatchKey:
    """The grouping key for one accumulating batch: one stream on one internal
    topic-partition (single-writer-per-stream, BW-7). MVP: one shard → one
    partition → one stream, so this is structurally one batch per partition.
    """

    topic: str
    partition: int
    stream_id: UUID
    workspace_id: UUID


@dataclass
class _Accumulator:
    """One in-flight batch being filled from polled records (BW-2)."""

    key: BatchKey
    events: list[EnvelopeMapping] = field(default_factory=list)
    first_offset: int = -1
    last_offset: int = -1

    def add(self, *, offset: int, envelope: EnvelopeMapping) -> None:
        if self.first_offset < 0:
            self.first_offset = offset
        self.last_offset = offset
        self.events.append(envelope)

    def to_batch(self) -> DeliveryBatch:
        return DeliveryBatch(
            workspace_id=self.key.workspace_id,
            stream_id=self.key.stream_id,
            topic=self.key.topic,
            partition=self.key.partition,
            first_offset=self.first_offset,
            last_offset=self.last_offset,
            events=self.events,
        )


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


class SinkHost:
    """The poll/batch/deliver/commit-after-insert loop for one channel (§3.5).

    ``deserialize`` turns record bytes into an envelope mapping (the internal shape,
    ``_df`` still present); the channel ``strip_internal``-s at ingest. ``arm_tenant``
    arms the per-batch workspace context before delivery so the channel's DB write
    is RLS-scoped (SINK-7) — the entrypoint injects the tenancy context manager;
    unit tests inject a no-op.
    """

    def __init__(
        self,
        consumer: KafkaConsumer,
        channel: DeliveryChannel,
        *,
        topics: list[str],
        deserialize: Callable[[bytes], EnvelopeMapping],
        arm_tenant: Callable[[UUID], AbstractContextManager[object]] | None = None,
        linger_ms: int = LINGER_MS,
        max_batch_events: int = MAX_BATCH_EVENTS,
    ) -> None:
        self._consumer = consumer
        self._channel = channel
        self._topics = topics
        self._deserialize = deserialize
        self._arm_tenant = arm_tenant
        self._linger_ms = linger_ms
        self._max_batch_events = max_batch_events
        self._stopped = False
        self._paused: dict[tuple[str, int], int] = {}  # (topic, partition) -> resume_at_ms
        self._backoff_ms: dict[tuple[str, int], int] = {}
        self.delivered_total = 0
        self.committed_through: dict[tuple[str, int], int] = {}

    def stop(self) -> None:
        """Request a clean stop after the current batch (called by the supervisor)."""
        self._stopped = True

    def start(self) -> None:
        """Subscribe to the delivery topic(s) and run until stopped (§3.5)."""
        self._consumer.subscribe(self._topics)
        logger.info("sink_host.subscribed", topics=self._topics)
        try:
            self.run()
        finally:
            self._shutdown()

    def run(self) -> None:
        """The poll/batch/deliver loop. One iteration drains a ≤ linger window."""
        while not self._stopped:
            self.run_once()

    # -- one batch window (BW-2) -------------------------------------------------

    def run_once(self) -> int:
        """Accumulate one ≤ 500-event / ≤ 250 ms window, deliver, commit. Returns
        the number of events delivered this window (0 on an empty poll).

        Records are grouped by (topic, partition, stream) so a single-stream MVP
        partition yields one batch; the loop is multi-stream-general for Phase 11.
        """
        self._resume_due_partitions()
        deadline = _now_ms() + self._linger_ms
        batches: OrderedDict[BatchKey, _Accumulator] = OrderedDict()
        count = 0
        while count < self._max_batch_events and _now_ms() < deadline:
            msg = self._consumer.poll(POLL_TIMEOUT_S)
            if msg is None:
                if batches:
                    break  # have something; deliver it rather than wait the full linger
                continue
            if msg.error():
                continue  # EOF / rebalance signal — not a record
            acc = self._accumulate(batches, msg)
            if acc is not None:
                count += 1
            if _now_ms() >= deadline:
                break
        delivered = 0
        for acc in batches.values():
            delivered += self._deliver_and_commit(acc)
        return delivered

    def _accumulate(
        self, batches: OrderedDict[BatchKey, _Accumulator], msg: ConsumerMessage
    ) -> _Accumulator | None:
        """Decode one record into its (topic, partition, stream) accumulator.

        A record on a paused partition is skipped (the host paused it for
        backpressure, SINK-8; the broker should not deliver it, but the guard keeps
        the contract local). A record that fails to deserialize is dropped with a
        warning — a malformed internal record is an upstream bug, not deliverable.
        """
        tp = (msg.topic(), msg.partition())
        if tp in self._paused:
            return None
        raw = msg.value()
        if raw is None:
            return None
        try:
            envelope = self._deserialize(raw)
        except Exception as exc:
            logger.warning(
                "sink_host.deserialize_failed",
                topic=msg.topic(),
                partition=msg.partition(),
                offset=msg.offset(),
                error=str(exc),
            )
            return None
        key = BatchKey(
            topic=msg.topic(),
            partition=msg.partition(),
            stream_id=UUID(str(envelope["stream_id"])),
            workspace_id=UUID(str(envelope["workspace_id"])),
        )
        acc = batches.get(key)
        if acc is None:
            acc = _Accumulator(key=key)
            batches[key] = acc
        acc.add(offset=msg.offset(), envelope=envelope)
        return acc

    def _deliver_and_commit(self, acc: _Accumulator) -> int:
        """Deliver one batch, then commit offsets up to ``acked_through`` (SINK-3).

        The commit is **after** the channel reports durability — for the buffer
        -writer, after the DB transaction commits (BW-3, INV-DEL-3). On
        ``backpressure`` the partition is paused + backed off (SINK-8) and *no*
        offset is committed (the range redelivers on resume). On ``fatal`` the host
        stops the binding (SINK-9).
        """
        if not acc.events:
            return 0
        batch = acc.to_batch()
        cm: AbstractContextManager[object] = (
            self._arm_tenant(batch.workspace_id) if self._arm_tenant else _NullCtx()
        )
        with cm:
            result = self._channel.deliver(batch)

        tp = (batch.topic, batch.partition)
        if result.status == "ok":
            self._reset_backoff(tp)
            if result.acked_through is not None:
                self._commit(batch.topic, batch.partition, result.acked_through)
                self.committed_through[tp] = result.acked_through
            self.delivered_total += batch.count
            return batch.count
        if result.status == "backpressure":
            self._apply_backpressure(tp, result.retry_after_ms)
            return 0
        # fatal — stop consuming for this binding (SINK-9).
        logger.error(
            "sink_host.fatal",
            topic=batch.topic,
            partition=batch.partition,
            error=str(result.error),
        )
        self._stopped = True
        return 0

    # -- offset commit (SINK-3: commit acked_through + 1) ------------------------

    def _commit(self, topic: str, partition: int, acked_through: int) -> None:
        """Commit ``acked_through + 1`` (Kafka commits the *next* offset to read)."""
        from runner.sinks.consumer import TopicPartitionOffset

        offset: TopicPartitionOffset = TopicPartitionOffset(
            topic=topic, partition=partition, offset=acked_through + 1
        )
        self._consumer.commit(offsets=[offset], asynchronous=False)
        logger.debug(
            "sink_host.committed",
            topic=topic,
            partition=partition,
            acked_through=acked_through,
        )

    # -- backpressure (SINK-8) ---------------------------------------------------

    def _apply_backpressure(self, tp: tuple[str, int], hint_ms: int | None) -> None:
        """Pause the partition + schedule a resume after an exponential backoff."""
        prev = self._backoff_ms.get(tp, 0)
        base = hint_ms if hint_ms is not None else clamp_backpressure_ms(prev * 2 or 100)
        backoff = clamp_backpressure_ms(max(base, prev * 2 or base))
        self._backoff_ms[tp] = backoff
        self._paused[tp] = _now_ms() + backoff
        self._consumer.pause([tp])
        logger.info("sink_host.paused", topic=tp[0], partition=tp[1], backoff_ms=backoff)

    def _resume_due_partitions(self) -> None:
        now = _now_ms()
        due = [tp for tp, at in self._paused.items() if at <= now]
        for tp in due:
            del self._paused[tp]
            self._consumer.resume([tp])
            logger.info("sink_host.resumed", topic=tp[0], partition=tp[1])

    def _reset_backoff(self, tp: tuple[str, int]) -> None:
        self._backoff_ms.pop(tp, None)  # reset on first ok (SINK-8)

    # -- shutdown (§3.5 rebalance/shutdown flush) --------------------------------

    def _shutdown(self) -> None:
        from contextlib import suppress

        with suppress(Exception):
            self._channel.flush("shutdown")
        with suppress(Exception):
            self._channel.close()
        with suppress(Exception):
            self._consumer.close()
        logger.info("sink_host.shutdown", delivered_total=self.delivered_total)


class _NullCtx:
    """A no-op context manager (tenancy arming is injected by the entrypoint)."""

    def __enter__(self) -> _NullCtx:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
