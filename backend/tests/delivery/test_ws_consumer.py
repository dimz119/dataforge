"""WebSocket tail consumer tests (delivery-channels §6; WS-1..WS-12).

The per-connection :class:`~delivery.api.consumers.StreamEventsConsumer` driven by the
Channels :class:`~channels.testing.WebsocketCommunicator` — no broker, no live Redis
(the in-memory channel layer + faked revocation cache configured in
``config.settings.test`` / ``tests/delivery/conftest``). Each test asserts one rung of
the §6 contract:

* WS-1 subprotocol negotiation → reject (no 101) on an unsupported subprotocol;
* WS-2 auth deadline → close 4408 when no auth frame arrives;
* WS-3 invalid/revoked → 4401, missing scope → 4403, foreign stream → 4404;
* the auth → ready → event happy path;
* WS-6 resume_ack behind-gap reporting;
* WS-10 drop_notice on send-queue overflow;
* §6.1 frame_seq monotonic delivery + WS-11 gap → drop_notice.

DB-touching auth runs in a thread (``database_sync_to_async``), so these use
``django_db(transaction=True)`` for cross-thread visibility on SQLite.
"""

from __future__ import annotations

from typing import Any

import pytest

# Import the communicator directly (channels.testing.__init__ pulls in daphne for the
# live-server case, which the unit lane does not need).
from channels.testing.websocket import WebsocketCommunicator

from delivery.api.consumers import StreamEventsConsumer
from delivery.domain.ws_protocol import (
    CLOSE_AUTH_FAILED,
    CLOSE_AUTH_TIMEOUT,
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    SUBPROTOCOL_V1,
)
from delivery.infra.ws_pusher_channel import WS_EVENT_MESSAGE_TYPE, ws_group_name

pytestmark = pytest.mark.django_db(transaction=True)


def _communicator(
    stream_id: str, *, subprotocols: list[str] | None = None
) -> WebsocketCommunicator:
    """A communicator wired to the consumer with a ``url_route`` + subprotocols scope."""
    app = StreamEventsConsumer.as_asgi()
    communicator = WebsocketCommunicator(
        app, f"/ws/streams/{stream_id}/events", subprotocols=subprotocols
    )
    communicator.scope["url_route"] = {"kwargs": {"stream_id": stream_id}}
    return communicator


async def _connect(communicator: WebsocketCommunicator) -> tuple[bool, Any]:
    result: tuple[bool, Any] = await communicator.connect()
    return result


# -- WS-1 subprotocol negotiation ---------------------------------------------


async def test_no_supported_subprotocol_rejects_handshake(ws_world: Any) -> None:
    communicator = _communicator(ws_world.stream_id, subprotocols=["something.else"])
    connected, _ = await _connect(communicator)
    assert connected is False  # no 101 → HTTP 400-class rejection (WS-1)
    await communicator.disconnect()


async def test_supported_subprotocol_accepts_and_echoes(ws_world: Any) -> None:
    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    connected, subprotocol = await _connect(communicator)
    assert connected is True
    assert subprotocol == SUBPROTOCOL_V1  # server selects + echoes (WS-1)
    await communicator.disconnect()


# -- WS-2 auth deadline -------------------------------------------------------


async def test_auth_deadline_closes_4408(ws_world: Any, monkeypatch: Any) -> None:
    # Compress the 10 s deadline so the test is fast; the close-code is the contract.
    monkeypatch.setattr("delivery.api.consumers.AUTH_DEADLINE_S", 0.2)
    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    out = await communicator.receive_output(timeout=2)
    assert out["type"] == "websocket.close"
    assert out["code"] == CLOSE_AUTH_TIMEOUT
    await communicator.disconnect()


# -- WS-3 auth failures -------------------------------------------------------


async def test_invalid_api_key_closes_4401(ws_world: Any) -> None:
    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await communicator.send_json_to({"type": "auth", "api_key": "df_dev_bogus_nope"})
    out = await communicator.receive_output(timeout=2)
    assert out["type"] == "websocket.close"
    assert out["code"] == CLOSE_AUTH_FAILED
    await communicator.disconnect()


async def test_missing_scope_closes_4403(make_user: Any, ws_revocation_store: Any) -> None:
    from asgiref.sync import sync_to_async

    from tests.delivery.ws_fixtures import build_ws_world

    # A key with NO events:read scope, in the stream's own workspace → 4403 (WS-3).
    world = await sync_to_async(build_ws_world)(
        make_user=make_user, label="NOSCOPE", scopes=["answer_key:read"]
    )
    communicator = _communicator(world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await communicator.send_json_to({"type": "auth", "api_key": world.api_key_plaintext})
    out = await communicator.receive_output(timeout=2)
    assert out["type"] == "websocket.close"
    assert out["code"] == CLOSE_FORBIDDEN
    await communicator.disconnect()


async def test_foreign_workspace_stream_closes_4404(
    make_user: Any, ws_revocation_store: Any
) -> None:
    from asgiref.sync import sync_to_async

    from tests.delivery.ws_fixtures import build_ws_world

    victim = await sync_to_async(build_ws_world)(make_user=make_user, label="VICTIM")
    attacker = await sync_to_async(build_ws_world)(make_user=make_user, label="ATTACKER")
    # Attacker's valid-but-foreign key against the victim's stream → 4404 (anti-enum).
    communicator = _communicator(victim.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await communicator.send_json_to({"type": "auth", "api_key": attacker.api_key_plaintext})
    out = await communicator.receive_output(timeout=2)
    assert out["type"] == "websocket.close"
    assert out["code"] == CLOSE_NOT_FOUND
    await communicator.disconnect()


async def test_unknown_stream_closes_4404(ws_world: Any) -> None:
    import uuid

    communicator = _communicator(str(uuid.uuid4()), subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await communicator.send_json_to({"type": "auth", "api_key": ws_world.api_key_plaintext})
    out = await communicator.receive_output(timeout=2)
    assert out["type"] == "websocket.close"
    assert out["code"] == CLOSE_NOT_FOUND
    await communicator.disconnect()


# -- auth → ready → event happy path ------------------------------------------


async def _auth_and_ready(
    communicator: WebsocketCommunicator, api_key: str
) -> dict[str, Any]:
    """Send a valid auth frame, return the ``ready`` frame."""
    await communicator.send_json_to({"type": "auth", "api_key": api_key})
    ready: dict[str, Any] = await communicator.receive_json_from(timeout=2)
    return ready


async def test_auth_ready_event_happy_path(ws_world: Any) -> None:
    from channels.layers import get_channel_layer

    from dataforge_engine.envelope import strip_internal
    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.domain.ws_protocol import SUBPROTOCOL_V1 as _P
    from delivery.domain.ws_protocol import build_event_frame

    communicator = _communicator(ws_world.stream_id, subprotocols=[_P])
    await _connect(communicator)
    ready = await _auth_and_ready(communicator, ws_world.api_key_plaintext)
    assert ready["type"] == "ready"
    assert ready["protocol"] == _P
    assert ready["stream_id"] == ws_world.stream_id
    assert ready["filters"]["sample_rate"] == 1.0
    assert ready["position"]["cursor"].startswith("c1.")

    # The ws-pusher fans an event into the stream group; the socket tails it (§6.1).
    layer = get_channel_layer()
    delivered = strip_internal(order_placed_envelope())
    frame = build_event_frame(cursor="c1.ignored", event=dict(delivered))
    await layer.group_send(
        ws_group_name(ws_world.stream_id),
        {"type": WS_EVENT_MESSAGE_TYPE, "frame_seq": 1, "frame": frame},
    )
    event = await communicator.receive_json_from(timeout=2)
    assert event["type"] == "event"
    assert event["event"]["event_type"] == "order_placed"
    assert not any(k.startswith("_df") for k in event["event"])  # SB-3
    assert event["cursor"].startswith("c1.")  # REST-interchangeable bookmark (WS-7)
    await communicator.disconnect()


async def test_types_filter_excludes_nonmatching_events(ws_world: Any) -> None:
    from channels.layers import get_channel_layer

    from dataforge_engine.envelope import strip_internal
    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.domain.ws_protocol import build_event_frame

    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    # Filter to a type the fixture event is NOT (WS-5).
    await communicator.send_json_to(
        {"type": "auth", "api_key": ws_world.api_key_plaintext, "types": ["cart_updated"]}
    )
    ready = await communicator.receive_json_from(timeout=2)
    assert ready["filters"]["types"] == ["cart_updated"]

    layer = get_channel_layer()
    frame = build_event_frame(cursor="c1.x", event=dict(strip_internal(order_placed_envelope())))
    await layer.group_send(
        ws_group_name(ws_world.stream_id),
        {"type": WS_EVENT_MESSAGE_TYPE, "frame_seq": 1, "frame": frame},
    )
    # The order_placed event is filtered out → nothing arrives (a heartbeat would be
    # the next frame, but it is 15 s away). Assert no event within a short window.
    assert await communicator.receive_nothing(timeout=0.5) is True
    await communicator.disconnect()


# -- WS-6 resume_ack + WS-8 expired cursor ------------------------------------


async def test_resume_at_tail_acks_behind_null(ws_world: Any) -> None:
    """A resume cursor at/ahead of the live tail → resume_ack with behind: null (WS-6).

    The empty buffer (no rows after the cursor) means the client is not behind; the
    socket never replays, it only re-positions.
    """
    from delivery.domain.cursor import encode_cursor
    from delivery.domain.ws_cursor import fingerprint_for

    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    fingerprint = fingerprint_for(stream_id=ws_world.stream_id, types=())
    cursor = encode_cursor(p=10**13, s=0, fingerprint=fingerprint)  # far-future tail
    await communicator.send_json_to(
        {"type": "auth", "api_key": ws_world.api_key_plaintext, "cursor": cursor}
    )
    ready = await communicator.receive_json_from(timeout=2)
    assert ready["type"] == "ready"
    ack = await communicator.receive_json_from(timeout=2)
    assert ack["type"] == "resume_ack"
    assert ack["behind"] is None  # at the tail → no gap (WS-6)
    assert ack["position"]["cursor"].startswith("c1.")
    await communicator.disconnect()


# -- WS-11 frame_seq gap → drop_notice + §6.1 monotonic delivery ---------------


async def test_frame_seq_gap_emits_drop_notice(ws_world: Any) -> None:
    """A skipped frame_seq (channel-layer drop) surfaces as a drop_notice (WS-11)."""
    from channels.layers import get_channel_layer

    from dataforge_engine.envelope import strip_internal
    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.domain.ws_protocol import build_event_frame

    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await _auth_and_ready(communicator, ws_world.api_key_plaintext)

    layer = get_channel_layer()
    group = ws_group_name(ws_world.stream_id)
    delivered = dict(strip_internal(order_placed_envelope()))

    def _msg(seq: int, cur: str) -> dict[str, Any]:
        return {
            "type": WS_EVENT_MESSAGE_TYPE,
            "frame_seq": seq,
            "frame": build_event_frame(cursor=cur, event=delivered),
        }

    # frame_seq 1, then a JUMP to 5 (frames 2-4 lost in the channel layer, WS-11).
    await layer.group_send(group, _msg(1, "c1.a"))
    first = await communicator.receive_json_from(timeout=2)
    assert first["type"] == "event"
    await layer.group_send(group, _msg(5, "c1.b"))
    # The next frames are a drop_notice (the gap) then the event (§6.1 / WS-11).
    frames = [await communicator.receive_json_from(timeout=2) for _ in range(2)]
    types = {f["type"] for f in frames}
    assert "drop_notice" in types
    drop = next(f for f in frames if f["type"] == "drop_notice")
    assert drop["dropped"] == 3  # frames 2,3,4 (INV-DEL-5: count is exact)
    await communicator.disconnect()


# -- WS-10 drop-oldest send-queue overflow → drop_notice ----------------------


async def test_send_queue_overflow_emits_drop_notice(monkeypatch: Any) -> None:
    """Overflowing the per-connection queue drops oldest + emits a drop_notice (WS-10).

    Drives the consumer's ``ws_event`` fan-in directly (no live sender draining) with a
    queue shrunk to 2 so a 5-frame burst overflows deterministically; then runs the
    sender once and asserts the drained drop_notice carries the count + the
    ``resume_cursor`` of the OLDEST dropped frame (the gap's lower bound, WS-10).
    """
    from dataforge_engine.envelope import strip_internal
    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.api.consumers import StreamEventsConsumer
    from delivery.application.ws_send_queue import DropOldestSendQueue
    from delivery.domain.ws_cursor import fingerprint_for
    from delivery.domain.ws_protocol import build_event_frame

    consumer = StreamEventsConsumer()
    consumer._closing = False
    consumer._types = ()
    consumer._sample_rate = 1.0
    consumer._dropped = 0
    consumer._last_frame_seq = None
    consumer._fingerprint = fingerprint_for(stream_id="s", types=())
    consumer._queue = DropOldestSendQueue(capacity=2)

    delivered = dict(strip_internal(order_placed_envelope()))
    # Five contiguous frames into a cap-2 queue → 3 oldest dropped (WS-10).
    for seq in range(1, 6):
        await consumer.ws_event(
            {
                "type": WS_EVENT_MESSAGE_TYPE,
                "frame_seq": seq,
                "frame": build_event_frame(cursor=f"c1.{seq}", event=delivered),
            }
        )
    assert consumer._queue.has_drops()
    count, resume_cursor = consumer._queue.drain_drop_notice()
    assert count == 3  # frames 1,2,3 dropped; 4,5 survive in the cap-2 queue
    assert resume_cursor is not None  # the position before the gap, for REST gap-fill
    assert len(consumer._queue) == 2


# -- WS-3 revoked key kills the live connection < 1 s -------------------------


async def test_revoked_key_disconnects_live_connection(
    ws_world: Any, ws_revocation_store: Any, monkeypatch: Any
) -> None:
    """Revoking the key mid-connection closes the socket 4401 within ~1 s (WS-3)."""
    monkeypatch.setattr("delivery.api.consumers._REVOCATION_POLL_S", 0.1)
    communicator = _communicator(ws_world.stream_id, subprotocols=[SUBPROTOCOL_V1])
    await _connect(communicator)
    await _auth_and_ready(communicator, ws_world.api_key_plaintext)

    # Plant a revocation for this key's prefix (the synchronous revoke path, ADR-0011).
    from tenancy.infra import revocation_cache

    ws_revocation_store[ws_world.api_key_prefix] = revocation_cache.STATE_REVOKED

    # Within a few poll cycles the watch closes the socket 4401.
    out = None
    for _ in range(20):
        out = await communicator.receive_output(timeout=1)
        if out["type"] == "websocket.close":
            break
    assert out is not None and out["type"] == "websocket.close"
    assert out["code"] == CLOSE_AUTH_FAILED
    await communicator.disconnect()
