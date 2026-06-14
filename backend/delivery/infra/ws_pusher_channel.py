"""The ``websocket`` ws-pusher :class:`~delivery.domain.channel.DeliveryChannel`
(delivery-channels §6.1; backend-architecture §8.6; ADR-0013).

The fan-out half of the WebSocket tail: given one ordered
:class:`~delivery.domain.channel.DeliveryBatch` from one internal topic-partition,
it ``strip_internal``-s every envelope at ingest (SB-2, the delivered 20-key shape),
stamps a **per-stream monotonic** ``frame_seq``, mints the REST-interchangeable
``event`` cursor (``delivery.domain.ws_cursor``), and fans each frame out to the
Redis channel-layer group ``stream_{stream_id}`` via ``channel_layer.group_send``.
The per-connection consumer (``delivery.api.consumers``) detects ``frame_seq`` gaps
(channel-layer capacity drops) and emits the explicit ``drop_notice`` frame
(INV-DEL-5) — the drop is never silent.

Ack model (§6.1): the pusher acks Kafka **immediately** after ``group_send``
(``acked_through = batch.last_offset``). At-most-once per connection is the contract,
so durability ends at the channel layer — slow sockets hurt only themselves and the
completeness path is REST (delivery-channels §3.6). There is **no Redis lease** — the
ws-pusher is a Kafka consumer-group member (``df.sink.websocket.v1``), the broker's
group coordinator does the assignment (§8.6).

This is a ``DeliveryChannel`` behind the §3 interface, hosted by the same generic
sink host (``runner.sinks.host.SinkHost``) as the buffer-writer: poll/batch/commit
and backpressure are the host's; one batch's fan-out is the channel's. ``group_send``
is async (Channels), so it is bridged with ``asgiref.sync.async_to_sync``; the host
loop is synchronous (the confluent_kafka consumer is C-driven).

Error classification (§3.4): a ``strip_internal`` invariant / ``workspace_id``
mismatch (SINK-7) is ``fatal_contract``; a transient channel-layer error is
``backpressure`` (retryable, SINK-8/9) — but per the at-most-once contract the
pusher prefers to drop-and-continue over pausing Kafka (a stalled WS sink must never
back up the shared delivery topic), so a transient group_send failure is logged and
the batch is acked: the lost frames surface to clients as a ``frame_seq`` gap →
``drop_notice`` (INV-DEL-5), exactly the channel-layer-drop path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from dataforge_engine.envelope import StripError, strip_internal
from delivery.domain.channel import DeliveryResult, SinkError, SinkHealth
from delivery.domain.ws_cursor import cursor_after_event, fingerprint_for
from delivery.domain.ws_protocol import build_event_frame

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dataforge_engine.envelope import DeliveredEnvelope
    from delivery.domain.channel import DeliveryBatch, FlushReason

logger = structlog.get_logger("dataforge.delivery.ws_pusher")

__all__ = [
    "WS_EVENT_MESSAGE_TYPE",
    "ChannelLayerSender",
    "WsPusherChannel",
    "ws_group_name",
]

# The channel-layer message ``type`` the per-connection consumer dispatches on
# (Channels routes ``{"type": "ws.event"}`` → ``consumer.ws_event``). Dots become
# underscores in the handler name (``ws.event`` → ``ws_event``).
WS_EVENT_MESSAGE_TYPE = "ws.event"


def ws_group_name(stream_id: str) -> str:
    """The channel-layer group for one stream's live tail (§6.1).

    Group names carry the stream UUID only — globally unique (INV-DEL-6) and
    ownership-checked at join (WS-3), so the group name is unguessable and tenant
    isolation rides on the auth gate, not the name.
    """
    return f"stream_{stream_id}"


class ChannelLayerSender:
    """A minimal async ``group_send`` seam (the slice of the Channels channel layer
    the pusher uses), bridged to sync for the host loop.

    The real adapter is :func:`get_default_sender`; unit tests inject a fake that
    records ``(group, message)`` pairs so the fan-out + ``frame_seq`` stamping is
    asserted without Redis.

    Threading model: the sink host calls ``deliver`` (hence ``group_send``) one batch
    at a time from a single worker thread (SINK-1), so this sender owns ONE persistent
    asyncio event loop and runs every ``group_send`` coroutine on it. This is the
    critical scalability fix vs. ``async_to_sync`` per call: ``async_to_sync`` spins up
    (and tears down) a fresh event loop each invocation, and ``channels-redis`` binds
    its connection pool to the running loop — so a new loop per message means a new
    Redis TCP connection per message. At sustained throughput that exhausts the
    ephemeral source-port range (``Error 99 EADDRNOTAVAIL``) and drops fan-out frames.
    One long-lived loop keeps the channel-layer connection pool warm and bounded.
    """

    def __init__(self, channel_layer: Any) -> None:
        self._layer = channel_layer
        self._loop: Any = None

    def _ensure_loop(self) -> Any:
        import asyncio

        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def group_send(self, group: str, message: Mapping[str, Any]) -> None:
        """Fan one channel-layer message to ``group`` on the persistent loop.

        Runs the async ``group_send`` to completion on this sender's own event loop so
        the ``channels-redis`` connection pool is reused across messages (no per-call
        loop/connection churn). Synchronous from the host's perspective (SINK-1).
        """
        loop = self._ensure_loop()
        loop.run_until_complete(self._layer.group_send(group, dict(message)))

    def close(self) -> None:
        """Tear down the persistent loop (host shutdown). Idempotent."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
        self._loop = None


def get_default_sender() -> ChannelLayerSender:
    """The production sender over the configured ``channels-redis`` layer (§10)."""
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:  # pragma: no cover - misconfiguration guard
        raise RuntimeError(
            "No channel layer configured (settings.CHANNEL_LAYERS['default'])."
        )
    return ChannelLayerSender(layer)


class WsPusherChannel:
    """The ``websocket`` channel (delivery-channels §3 + §6.1).

    Per-stream ``frame_seq`` counters survive across batches (keyed by ``stream_id``),
    starting at 1; the per-connection consumer treats the first observed ``frame_seq``
    as its baseline and only a *gap* (a skipped value) is a drop. ``deliver`` is
    synchronous (the host guarantees one ``deliver`` in flight per topic-partition,
    SINK-1) and bridges each ``group_send`` to async.
    """

    channel_type: ClassVar[str] = "websocket"

    def __init__(self, sender: ChannelLayerSender | None = None) -> None:
        self._sender = sender
        self._frame_seq: dict[str, int] = {}
        self._healthy = True
        self._health_detail = ""

    # -- DeliveryChannel: control-plane validation (§3.1) ------------------------

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> list[Any]:
        """The platform-shared ws-pusher takes no per-binding config (§6.1): one
        group over the single delivery topic. Always valid."""
        return []

    def configure(self, binding: Any, secrets: Any) -> None:
        """Bind the channel-layer sender (side-effect-free beyond connection setup,
        §3.1). Lazily resolves the production sender on first delivery if none was
        injected, so unit construction stays Django/Redis-free."""

    # -- DeliveryChannel: deliver one batch (§3.1, SINK-2/7; §6.1 fan-out) -------

    def deliver(self, batch: DeliveryBatch) -> DeliveryResult:
        """Strip → stamp ``frame_seq`` → mint REST cursor → ``group_send`` → ack.

        At-most-once (§6.1): acks ``batch.last_offset`` immediately after fan-out. A
        contract violation (strip / SINK-7) is ``fatal``; a transient channel-layer
        error is logged and the batch is still acked (the lost frames surface as a
        ``frame_seq`` gap → ``drop_notice``, INV-DEL-5) so a stalled WS sink never
        backs up the shared delivery topic.
        """
        if batch.count == 0:
            return DeliveryResult.ok(acked_through=batch.last_offset)

        try:
            delivered = self._strip_and_attribute(batch)
        except StripError as exc:
            return self._fatal_contract("strip_internal invariant failed", exc)
        except _WorkspaceMismatch as exc:
            return self._fatal_contract("workspace_id mismatch (SINK-7)", exc)

        sid = str(batch.stream_id)
        group = ws_group_name(sid)
        # The fingerprint is over the *unfiltered* stream (filter set ""): the pusher
        # fans every event to the group; per-connection filtering + per-connection
        # filter-bound cursors are the consumer's job (RC-4 narrows delivery, never
        # renumbers). The event cursor here is the unfiltered REST position.
        fingerprint = fingerprint_for(stream_id=sid, types=())
        sender = self._sender_or_default()

        sent = 0
        for env in delivered:
            frame_seq = self._next_frame_seq(sid)
            cursor = cursor_after_event(envelope=env, fingerprint=fingerprint)
            frame = build_event_frame(cursor=cursor, event=env)
            message = {
                "type": WS_EVENT_MESSAGE_TYPE,
                "frame_seq": frame_seq,
                "frame": frame,
            }
            try:
                sender.group_send(group, message)
            except Exception as exc:
                # Transient channel-layer error: do NOT pause Kafka (at-most-once,
                # §6.1). The skipped frame_seq is the gap the consumer reports as a
                # drop_notice (INV-DEL-5). Mark unhealthy for /readyz visibility.
                self._healthy = False
                self._health_detail = f"group_send failed: {exc}"
                logger.warning(
                    "ws_pusher.group_send_failed",
                    stream_id=sid,
                    frame_seq=frame_seq,
                    error=str(exc),
                )
                continue
            sent += 1

        if sent > 0:
            self._healthy = True
            self._health_detail = ""
        logger.debug(
            "ws_pusher.delivered",
            stream_id=sid,
            group=group,
            frames=sent,
            acked_through=batch.last_offset,
        )
        # Ack immediately (§6.1 at-most-once): durability ends at the channel layer.
        return DeliveryResult.ok(acked_through=batch.last_offset)

    def _strip_and_attribute(self, batch: DeliveryBatch) -> list[DeliveredEnvelope]:
        """``strip_internal`` every envelope (SB-2) + enforce SINK-7 attribution.

        Each envelope's ``workspace_id``/``stream_id`` must match the batch's
        authoritative attribution; a mismatch is a fatal contract violation (SINK-7).
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

    def _next_frame_seq(self, stream_id: str) -> int:
        """The next per-stream monotonic ``frame_seq`` (§6.1), starting at 1."""
        nxt = self._frame_seq.get(stream_id, 0) + 1
        self._frame_seq[stream_id] = nxt
        return nxt

    def _sender_or_default(self) -> ChannelLayerSender:
        if self._sender is None:
            self._sender = get_default_sender()
        return self._sender

    # -- DeliveryChannel: flush / health / close (§3.1) --------------------------

    def flush(self, reason: FlushReason) -> DeliveryResult:
        """No sink-internal staging — every ``deliver`` already fanned out (§6.1).

        The channel layer is the durability boundary (at-most-once), so ``flush`` is a
        no-op reporting ``ok`` (SINK-6)."""
        return DeliveryResult.ok(acked_through=None)

    def healthcheck(self) -> SinkHealth:
        """Liveness for ``/readyz`` (§3.1) — healthy unless the last fan-out failed."""
        return SinkHealth(healthy=self._healthy, detail=self._health_detail)

    def close(self) -> None:
        """Release per-stream counters (the channel layer is host/process-owned)."""
        self._frame_seq.clear()

    # -- helpers -----------------------------------------------------------------

    def _fatal_contract(self, message: str, cause: Exception) -> DeliveryResult:
        self._healthy = False
        self._health_detail = message
        logger.error("ws_pusher.fatal_contract", message=message, error=str(cause))
        return DeliveryResult.fatal(
            SinkError(error_class="fatal_contract", message=message, cause=str(cause))
        )


class _WorkspaceMismatch(ValueError):
    """A batch envelope's tenant attribution disagrees with the batch (SINK-7)."""
