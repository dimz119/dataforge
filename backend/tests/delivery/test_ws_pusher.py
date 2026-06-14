"""ws-pusher sink tests (delivery-channels §6.1; backend-architecture §8.6).

The :class:`~delivery.infra.ws_pusher_channel.WsPusherChannel` against a *fake*
channel-layer sender (records ``(group, message)`` pairs) — no Redis, no broker, no
DB. Proves the §6.1 fan-out contract the consumer relies on:

* **strip at ingest (SB-2).** Every fanned event is the delivered 20-key shape; no
  ``_df``-prefixed key escapes.
* **monotonic per-stream ``frame_seq`` (§6.1).** Each fanned frame carries the next
  ``frame_seq`` so the per-connection consumer detects channel-layer gaps.
* **REST-interchangeable cursor (WS-7).** Each ``event`` frame's ``cursor`` decodes
  against the stream's filter fingerprint (the REST handoff position space).
* **immediate ack (§6.1 at-most-once).** ``deliver`` acks ``batch.last_offset``.
* **SINK-7 attribution.** A foreign ``workspace_id`` in an envelope is ``fatal``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from dataforge_engine.envelope.tests.fixtures import (
    STREAM_ID,
    WORKSPACE_ID,
    order_placed_envelope,
)
from delivery.domain.channel import DeliveryBatch
from delivery.domain.cursor import decode_cursor
from delivery.domain.ws_cursor import fingerprint_for
from delivery.infra.ws_pusher_channel import (
    ChannelLayerSender,
    WsPusherChannel,
    ws_group_name,
)


class _FakeLayer:
    """A fake channel layer recording every ``group_send`` (sync via the sender)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def group_send(self, group: str, message: dict[str, Any]) -> None:
        self.sent.append((group, message))


def _channel() -> tuple[WsPusherChannel, _FakeLayer]:
    layer = _FakeLayer()
    return WsPusherChannel(sender=ChannelLayerSender(layer)), layer


def _batch(*, offset: int, count: int = 1) -> DeliveryBatch:
    events = [order_placed_envelope() for _ in range(count)]
    return DeliveryBatch(
        workspace_id=UUID(WORKSPACE_ID),
        stream_id=UUID(STREAM_ID),
        topic="df.delivery.events.v1",
        partition=0,
        first_offset=offset,
        last_offset=offset + count - 1,
        events=events,
    )


def test_deliver_fans_out_and_acks_last_offset() -> None:
    channel, layer = _channel()
    result = channel.deliver(_batch(offset=7))
    assert result.status == "ok"
    assert result.acked_through == 7  # immediate ack (§6.1 at-most-once)
    assert len(layer.sent) == 1
    group, message = layer.sent[0]
    assert group == ws_group_name(STREAM_ID)
    assert message["type"] == "ws.event"
    assert message["frame"]["type"] == "event"


def test_strip_internal_at_ingest_no_df_key() -> None:
    channel, layer = _channel()
    channel.deliver(_batch(offset=0))
    event = layer.sent[0][1]["frame"]["event"]
    assert not any(k.startswith("_df") for k in event)  # SB-2 / SB-3
    assert len(event) == 20  # the delivered field set


def test_frame_seq_is_monotonic_per_stream() -> None:
    channel, layer = _channel()
    channel.deliver(_batch(offset=0))
    channel.deliver(_batch(offset=1))
    channel.deliver(_batch(offset=2, count=2))
    seqs = [m["frame_seq"] for _, m in layer.sent]
    assert seqs == [1, 2, 3, 4]  # gapless, monotonic (§6.1)


def test_event_cursor_is_rest_decodable() -> None:
    channel, layer = _channel()
    channel.deliver(_batch(offset=0))
    cursor = layer.sent[0][1]["frame"]["cursor"]
    # The unfiltered-stream fingerprint the pusher mints against (RC-7).
    fingerprint = fingerprint_for(stream_id=STREAM_ID, types=())
    position = decode_cursor(cursor, expected_fingerprint=fingerprint)
    assert position.p > 0
    assert position.s >= 0


def test_foreign_workspace_envelope_is_fatal_contract() -> None:
    channel, _ = _channel()
    foreign = order_placed_envelope()
    foreign = {**foreign, "workspace_id": "00000000-0000-0000-0000-0000deadbeef"}
    batch = DeliveryBatch(
        workspace_id=UUID(WORKSPACE_ID),
        stream_id=UUID(STREAM_ID),
        topic="df.delivery.events.v1",
        partition=0,
        first_offset=0,
        last_offset=0,
        events=[foreign],
    )
    result = channel.deliver(batch)
    assert result.status == "fatal"
    assert result.error is not None
    assert result.error.error_class == "fatal_contract"


def test_empty_batch_acks_without_fanout() -> None:
    channel, layer = _channel()
    batch = DeliveryBatch(
        workspace_id=UUID(WORKSPACE_ID),
        stream_id=UUID(STREAM_ID),
        topic="df.delivery.events.v1",
        partition=0,
        first_offset=-1,
        last_offset=-1,
        events=[],
    )
    result = channel.deliver(batch)
    assert result.status == "ok"
    assert layer.sent == []


def test_group_send_error_does_not_pause_kafka() -> None:
    """A transient channel-layer error drops+continues (at-most-once) and still
    acks — a stalled WS sink must never back up the shared delivery topic (§6.1)."""

    class _BrokenLayer:
        async def group_send(self, group: str, message: dict[str, Any]) -> None:
            raise RuntimeError("channel layer down")

    channel = WsPusherChannel(sender=ChannelLayerSender(_BrokenLayer()))
    result = channel.deliver(_batch(offset=3))
    assert result.status == "ok"  # acked despite the fan-out failure (INV-DEL-5 gap)
    assert result.acked_through == 3
    assert channel.healthcheck().healthy is False
