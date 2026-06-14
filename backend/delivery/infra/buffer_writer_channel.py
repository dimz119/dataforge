"""The ``rest_buffer`` buffer-writer :class:`~delivery.domain.channel.DeliveryChannel`
(delivery-channels §4; database-schema §6.1; ADR-0013).

This is the durability half of the buffer-writer sink: given one ordered
:class:`~delivery.domain.channel.DeliveryBatch` from one internal topic-partition,
it ``strip_internal``-s every envelope at ingest (SB-2, the delivered 20-key shape),
then writes the batch transactionally into ``event_buffer`` via
:class:`~delivery.infra.buffer_store.BufferStore`, assigning the per-stream
monotonic ``buffer_seq`` (BW-6). It returns ``acked_through = batch.last_offset``
only after the DB transaction commits (BW-3, at-least-once INV-DEL-3) so the host
commits Kafka offsets *after* the insert.

The Kafka consumer-group harness (poll/batch/commit/rebalance, ``df.sink.rest
-buffer.v1``) lives in ``runner.sinks`` — the data-plane host. This class is the
channel: pure delivery logic over the Django ORM, no broker knowledge (SINK-10).

Error classification (§3.4):

* a contract failure (``strip_internal`` invariant, ``workspace_id`` mismatch,
  SINK-7) → ``fatal_contract`` (release-blocking; upstream validation should make
  it unreachable);
* a Postgres-unavailable / transient write error → ``backpressure`` (retryable,
  SINK-8/SINK-9) — the host pauses the partition and retries; never data loss.

The writer never deduplicates on ``event_id`` (BW-4): chaos duplicates are distinct
delivered instances and are all stored (SINK-4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from dataforge_engine.envelope import StripError, strip_internal
from delivery.domain.channel import (
    DeliveryResult,
    SinkError,
    SinkHealth,
    clamp_backpressure_ms,
)
from delivery.infra.buffer_store import BufferStore
from delivery.infra.stream_stats import record_delivered_batch

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dataforge_engine.envelope import DeliveredEnvelope
    from delivery.domain.channel import DeliveryBatch, FlushReason

logger = structlog.get_logger("dataforge.delivery.buffer_writer")

__all__ = ["RETRY_HINT_MS", "BufferWriterChannel"]

# Initial backpressure hint on a retryable write failure (host grows it x2, SINK-8).
RETRY_HINT_MS = 500


class BufferWriterChannel:
    """The ``rest_buffer`` channel (delivery-channels §3 + §4).

    One :class:`~delivery.infra.buffer_store.BufferStore` per stream (single writer,
    BW-7), created lazily on first delivery and keyed by ``stream_id`` so the
    per-stream ``buffer_seq`` counter survives across batches. ``deliver`` is
    synchronous and writes one batch per call; the host guarantees exactly one
    ``deliver`` in flight per (instance, topic-partition) (SINK-1).
    """

    channel_type: ClassVar[str] = "rest_buffer"

    def __init__(self) -> None:
        self._stores: dict[str, BufferStore] = {}
        self._healthy = True
        self._health_detail = ""

    # -- DeliveryChannel: control-plane validation (§3.1) ------------------------

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> list[Any]:
        """The platform-shared buffer-writer takes no per-binding config (§4.1):
        one group over the single delivery topic. Always valid.
        """
        return []

    def configure(self, binding: Any, secrets: Any) -> None:
        """No external resource to connect — the Django ``default`` connection is
        the buffer's durability target. Side-effect-free (§3.1).
        """

    # -- DeliveryChannel: deliver one batch (§3.1, SINK-2/3/7) -------------------

    def deliver(self, batch: DeliveryBatch) -> DeliveryResult:
        """Strip → transactional COPY → ack through the batch's last offset.

        Idempotent on the transport boundary (SINK-4): a redelivered offset range
        is re-appended under fresh ``buffer_seq`` (BW-3, licensed at-least-once
        duplicate); rows are never deduplicated on ``event_id`` (BW-4).
        """
        if batch.count == 0:
            return DeliveryResult.ok(acked_through=batch.last_offset)

        try:
            delivered = self._strip_and_attribute(batch)
        except StripError as exc:
            return self._fatal_contract("strip_internal invariant failed", exc)
        except _WorkspaceMismatch as exc:
            return self._fatal_contract("workspace_id mismatch (SINK-7)", exc)

        store = self._store_for(batch)
        try:
            result = store.write_batch(delivered)
        except Exception as exc:
            # Retryable: Postgres unavailable / transient. Surface as backpressure
            # (SINK-8/9); the host pauses + retries the same offsets — no loss.
            self._healthy = False
            self._health_detail = f"write failed: {exc}"
            logger.warning(
                "buffer_writer.write_failed",
                stream_id=str(batch.stream_id),
                first_offset=batch.first_offset,
                last_offset=batch.last_offset,
                error=str(exc),
            )
            return DeliveryResult.backpressure(
                retry_after_ms=clamp_backpressure_ms(RETRY_HINT_MS)
            )

        self._healthy = True
        self._health_detail = ""
        # StreamStats: the buffer-writer is the canonical counting point (observability
        # §5) — count exactly the rows now durable in event_buffer, AFTER the commit,
        # so the Redis tally reconciles byte-for-byte with REST replay (the XCH exit
        # criterion). Fails open (a Redis miss never fails a delivery; INV-OBS-2
        # rebuildable). Runs inside the host's per-batch armed workspace context.
        record_delivered_batch(
            workspace_id=str(batch.workspace_id),
            stream_id=str(batch.stream_id),
            envelopes=delivered,
        )
        logger.debug(
            "buffer_writer.delivered",
            stream_id=str(batch.stream_id),
            rows=result.rows_written,
            first_buffer_seq=result.first_buffer_seq,
            last_buffer_seq=result.last_buffer_seq,
            acked_through=batch.last_offset,
        )
        # acked_through only after the txn committed (BW-3, INV-DEL-3).
        return DeliveryResult.ok(acked_through=batch.last_offset)

    def _strip_and_attribute(self, batch: DeliveryBatch) -> list[DeliveredEnvelope]:
        """``strip_internal`` every envelope (SB-2) + enforce SINK-7 attribution.

        Each envelope's ``workspace_id``/``stream_id`` must match the batch's
        authoritative attribution; a mismatch is a fatal contract violation
        (SINK-7) — upstream validation should make it unreachable.
        """
        ws = str(batch.workspace_id)
        sid = str(batch.stream_id)
        delivered: list[DeliveredEnvelope] = []
        for env in batch.events:
            if str(env.get("workspace_id")) != ws or str(env.get("stream_id")) != sid:
                raise _WorkspaceMismatch(
                    f"envelope ({env.get('workspace_id')}/{env.get('stream_id')}) "
                    f"disagrees with batch ({ws}/{sid})"
                )
            delivered.append(strip_internal(env))  # SB-2: exactly once at ingest
        return delivered

    def _store_for(self, batch: DeliveryBatch) -> BufferStore:
        key = str(batch.stream_id)
        store = self._stores.get(key)
        if store is None:
            store = BufferStore(
                workspace_id=str(batch.workspace_id), stream_id=str(batch.stream_id)
            )
            self._stores[key] = store
        return store

    # -- DeliveryChannel: flush / health / close (§3.1) --------------------------

    def flush(self, reason: FlushReason) -> DeliveryResult:
        """No sink-internal staging — every ``deliver`` is already durable (BW-3).

        The buffer-writer commits per batch, so ``acked_through`` always equals the
        last delivered offset; ``flush`` is a no-op that reports ``ok`` (SINK-6).
        On revocation the host then commits and releases the partition.
        """
        return DeliveryResult.ok(acked_through=None)

    def healthcheck(self) -> SinkHealth:
        """Liveness for ``/readyz`` (§3.1) — healthy unless the last write failed."""
        return SinkHealth(healthy=self._healthy, detail=self._health_detail)

    def close(self) -> None:
        """Release per-stream stores (the Django connection is host-owned)."""
        self._stores.clear()

    # -- helpers -----------------------------------------------------------------

    def _fatal_contract(self, message: str, cause: Exception) -> DeliveryResult:
        self._healthy = False
        self._health_detail = message
        logger.error("buffer_writer.fatal_contract", message=message, error=str(cause))
        return DeliveryResult.fatal(
            SinkError(error_class="fatal_contract", message=message, cause=str(cause))
        )


class _WorkspaceMismatch(ValueError):
    """A batch envelope's tenant attribution disagrees with the batch (SINK-7)."""
