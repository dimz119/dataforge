"""WS protocol + send-queue + cursor unit tests (delivery-channels §6).

Pure unit coverage of the framework-light pieces — the frame catalog (§6.3), the
close-code table (§6.5), the drop-oldest send queue (WS-10), and the
REST-interchangeable WS cursor (WS-7) — with no socket, no Redis, no DB.
"""

from __future__ import annotations

from dataforge_engine.envelope.tests.fixtures import STREAM_ID, order_placed_envelope
from delivery.application.ws_send_queue import DropOldestSendQueue, QueuedFrame
from delivery.domain.cursor import decode_cursor
from delivery.domain.ws_cursor import (
    canonical_filter_set,
    cursor_after_event,
    fingerprint_for,
)
from delivery.domain.ws_protocol import (
    CLOSE_AUTH_FAILED,
    CLOSE_AUTH_TIMEOUT,
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    CLOSE_PROTOCOL_VIOLATION,
    CLOSE_QUOTA_EXCEEDED,
    SEND_QUEUE_CAP,
    SUBPROTOCOL_V1,
    build_drop_notice_frame,
    build_event_frame,
    build_heartbeat_frame,
    build_ready_frame,
    build_resume_ack_frame,
)


def test_close_code_table_matches_spec() -> None:
    # §6.5 / api-spec §5.5 close-code table — pinned values.
    assert CLOSE_PROTOCOL_VIOLATION == 4400
    assert CLOSE_AUTH_FAILED == 4401
    assert CLOSE_FORBIDDEN == 4403
    assert CLOSE_NOT_FOUND == 4404
    assert CLOSE_AUTH_TIMEOUT == 4408
    assert CLOSE_QUOTA_EXCEEDED == 4429


def test_subprotocol_token() -> None:
    assert SUBPROTOCOL_V1 == "dataforge.events.v1"


def test_frame_builders_shapes() -> None:
    ready = build_ready_frame(
        stream_id=STREAM_ID, cursor="c1.x", types=["a"], sample_rate=1.0
    )
    assert ready["type"] == "ready"
    assert ready["protocol"] == SUBPROTOCOL_V1
    assert ready["position"] == {"cursor": "c1.x"}
    assert ready["filters"] == {"types": ["a"], "sample_rate": 1.0}

    ack = build_resume_ack_frame(cursor="c1.y", behind={"events": 5, "from_cursor": "c1.z"})
    assert ack["type"] == "resume_ack"
    assert ack["behind"] == {"events": 5, "from_cursor": "c1.z"}
    assert build_resume_ack_frame(cursor="c1.y", behind=None)["behind"] is None

    ev = build_event_frame(cursor="c1.c", event={"event_id": "e"})
    assert ev == {"type": "event", "cursor": "c1.c", "event": {"event_id": "e"}}

    hb = build_heartbeat_frame(
        server_time="2026-06-14T00:00:00.000000Z", last_cursor="c1.l", delivered=3, dropped=1
    )
    assert hb["type"] == "heartbeat"
    assert hb["delivered"] == 3 and hb["dropped"] == 1

    dn = build_drop_notice_frame(dropped=250, resume_cursor="c1.r")
    assert dn == {"type": "drop_notice", "dropped": 250, "resume_cursor": "c1.r"}


# -- drop-oldest send queue (WS-10) -------------------------------------------


def test_send_queue_drops_oldest_on_overflow() -> None:
    q = DropOldestSendQueue(capacity=3)
    for i in range(3):
        q.put(QueuedFrame(frame={"n": i}, resume_cursor=f"c{i}"))
    assert not q.has_drops()
    # Overflow: the oldest (n=0) is dropped; its cursor is the gap's lower bound.
    q.put(QueuedFrame(frame={"n": 3}, resume_cursor="c3"))
    assert q.has_drops()
    count, resume_cursor = q.drain_drop_notice()
    assert count == 1
    assert resume_cursor == "c0"  # the position before the gap (WS-10)
    assert len(q) == 3


def test_send_queue_drain_resets_drop_state() -> None:
    q = DropOldestSendQueue(capacity=1)
    q.put(QueuedFrame(frame={"n": 0}, resume_cursor="c0"))
    q.put(QueuedFrame(frame={"n": 1}, resume_cursor="c1"))  # drops c0
    count, cursor = q.drain_drop_notice()
    assert (count, cursor) == (1, "c0")
    assert not q.has_drops()
    assert q.drain_drop_notice() == (0, None)


async def test_send_queue_get_is_fifo() -> None:
    q = DropOldestSendQueue(capacity=10)
    q.put(QueuedFrame(frame={"n": 0}, resume_cursor=None))
    q.put(QueuedFrame(frame={"n": 1}, resume_cursor=None))
    first = await q.get()
    second = await q.get()
    assert first.frame["n"] == 0
    assert second.frame["n"] == 1


def test_default_queue_capacity_is_1000() -> None:
    assert SEND_QUEUE_CAP == 1000
    assert len(DropOldestSendQueue()) == 0


# -- REST-interchangeable cursor (WS-7) ---------------------------------------


def test_ws_cursor_decodes_against_stream_fingerprint() -> None:
    fingerprint = fingerprint_for(stream_id=STREAM_ID, types=())
    cursor = cursor_after_event(envelope=order_placed_envelope(), fingerprint=fingerprint)
    position = decode_cursor(cursor, expected_fingerprint=fingerprint)
    assert position.f == fingerprint
    assert position.s == 48213  # the fixture's sequence_no


def test_ws_canonical_filter_set_matches_rest() -> None:
    # The WS canonicalization must agree with the REST service's so a WS cursor and
    # a REST cursor over the same (stream, filter set) share one fingerprint (RC-7).
    from delivery.application.services import canonical_filter_set as rest_canonical

    for types in (["b", "a"], ["a", "b", "a"], [], ["x"]):
        assert canonical_filter_set(types) == rest_canonical(types)


def test_ws_filter_fingerprint_matches_rest_service() -> None:
    from delivery.application.services import canonical_filter_set as rest_canonical
    from delivery.domain.cursor import filter_fingerprint

    types = ["order_placed", "cart_updated"]
    ws_fp = fingerprint_for(stream_id=STREAM_ID, types=types)
    rest_fp = filter_fingerprint(
        stream_id=STREAM_ID, canonical_filter_set=rest_canonical(types)
    )
    assert ws_fp == rest_fp  # cross-channel cursor interchange (WS-7)
