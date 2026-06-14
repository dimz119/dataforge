"""The per-connection WebSocket tail consumer (delivery-channels §6; api-spec §5;
backend-architecture §10).

``StreamEventsConsumer`` serves ``/ws/streams/{stream_id}/events`` on the dedicated
``ws`` ASGI process group. It implements WS-1..WS-12 exactly:

* **WS-1** subprotocol negotiation: accept ``dataforge.events.v1`` and echo it; a
  handshake offering no supported subprotocol is rejected at the HTTP level (no 101).
* **WS-2** first-message auth: the first frame MUST be ``auth`` within 10 s, else
  close ``4408``; credentials are an ``api_key`` (``events:read``) or a console
  ``access_token`` (workspace member) read from the frame body only (never the URL).
* **WS-3** failures: invalid/revoked → ``4401``, missing scope → ``4403``,
  foreign/unknown stream → ``4404``; a revoked key kills the live connection < 1 s.
* **WS-4** quotas: 5/key, 250/workspace → ``4429``.
* **WS-5** filters: ``types`` (≤ 20) + ``sample_rate`` set in the auth frame.
* **WS-6/7/8** resume: the socket never replays; a ``cursor`` yields ``resume_ack``
  with the ``behind`` gap; the client REST-fills; an expired cursor → non-fatal
  ``error`` and the tail continues.
* **WS-10/11** backpressure: a 1,000-frame drop-oldest queue + a ``frame_seq``-gap
  detector, both surfaced as ``drop_notice`` (INV-DEL-5).
* **WS-12** liveness: ``heartbeat`` every 15 s; 90 s socket silence → ``1001``.

The DB-touching work (auth resolution, stream ownership) runs through
``database_sync_to_async``; the fan-in is the channel-layer group ``stream_{id}``
joined only after auth (tenant isolation rides on the auth gate, INV-DEL-6).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import structlog
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from delivery.application.ws_auth import resolve_ws_auth
from delivery.application.ws_send_queue import DropOldestSendQueue, QueuedFrame
from delivery.domain.ws_cursor import cursor_after_event, fingerprint_for
from delivery.domain.ws_protocol import (
    AUTH_DEADLINE_S,
    CLOSE_AUTH_TIMEOUT,
    CLOSE_PROTOCOL_VIOLATION,
    HEARTBEAT_INTERVAL_S,
    MAX_TYPES_FILTER,
    SEND_QUEUE_CAP,
    SILENCE_TIMEOUT_S,
    SUBPROTOCOL_V1,
    build_drop_notice_frame,
    build_error_frame,
    build_heartbeat_frame,
    build_ready_frame,
    build_resume_ack_frame,
)
from delivery.infra.ws_pusher_channel import ws_group_name

logger = structlog.get_logger("dataforge.delivery.ws_consumer")

__all__ = ["StreamEventsConsumer"]

# How often the revoked-key watch polls the revocation cache (< 1 s disconnect, WS-3).
_REVOCATION_POLL_S = 0.5


def _json_default(value: Any) -> str:
    """JSON ``default`` hook: render ``Decimal`` as its literal digit string (S-6).

    The only non-JSON-native type the delivered envelope carries is ``Decimal``
    (monetary/seed amounts, event-model S-6) — rendered as a decimal string to match
    the canonical delivered shape the REST channel serializes (XCH-1).
    """
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class StreamEventsConsumer(AsyncJsonWebsocketConsumer):  # type: ignore[misc]
    """One live tail connection (delivery-channels §6).

    (``AsyncJsonWebsocketConsumer`` is ``Any`` to mypy — Channels ships no stubs —
    so the subclass-of-``Any`` strict error is suppressed; the seams are narrow.)
    """

    # The per-connection send-queue cap (WS-10). A class attribute so a test can
    # shrink it to exercise drop-oldest deterministically without flooding 1,000+
    # frames; production uses the §6.5 contract value (SEND_QUEUE_CAP = 1000).
    queue_capacity: int = SEND_QUEUE_CAP

    @classmethod
    async def encode_json(cls, content: Any) -> str:
        """Render an S→C frame to JSON text, S-6-faithful for delivered envelopes.

        The delivered envelope carries monetary/seed amounts in memory as ``Decimal``;
        the canonical delivered shape renders them as decimal **strings** (event-model
        S-6), exactly what the REST channel serializes — so the WS ``event.event``
        payload is byte-content-identical to the same instance's REST ``data[]`` entry
        (XCH-1). The default ``json.dumps`` cannot serialize ``Decimal``; this default
        hook renders it as ``str(value)`` (its literal digits), preserving scale.
        """
        import json

        return json.dumps(content, default=_json_default, separators=(",", ":"))

    async def connect(self) -> None:
        """WS-1 subprotocol negotiation + start the WS-2 auth deadline.

        Reject (no 101 → HTTP 400-class handshake failure) when the client offers no
        supported subprotocol; otherwise accept echoing ``dataforge.events.v1`` and
        arm the 10 s auth-frame deadline. Nothing is joined until auth succeeds.
        """
        self._authed = False
        self._stream_id = str(self.scope["url_route"]["kwargs"]["stream_id"])
        self._workspace_id: str | None = None
        self._key_prefix: str | None = None
        self._connection_id = uuid.uuid4().hex
        self._slot: Any = None
        self._fingerprint = fingerprint_for(stream_id=self._stream_id, types=())
        self._types: tuple[str, ...] = ()
        self._sample_rate = 1.0
        self._delivered = 0
        self._dropped = 0
        self._last_cursor: str | None = None
        self._last_frame_seq: int | None = None
        self._queue = DropOldestSendQueue(capacity=self.queue_capacity)
        self._tasks: list[asyncio.Task[Any]] = []
        self._last_client_activity = time.monotonic()
        self._closing = False

        offered = self.scope.get("subprotocols") or []
        if SUBPROTOCOL_V1 not in offered:
            # WS-1: no supported subprotocol → reject the handshake (no accept).
            await self.close(code=CLOSE_PROTOCOL_VIOLATION)
            return
        await self.accept(subprotocol=SUBPROTOCOL_V1)
        self._auth_deadline_task = asyncio.create_task(self._auth_deadline())

    async def _auth_deadline(self) -> None:
        """Close ``4408`` if no valid ``auth`` frame arrives within 10 s (WS-2)."""
        try:
            await asyncio.sleep(AUTH_DEADLINE_S)
        except asyncio.CancelledError:
            return
        if not self._authed and not self._closing:
            await self._shutdown(CLOSE_AUTH_TIMEOUT)

    async def disconnect(self, code: int) -> None:
        """Release the connection slot + cancel all background tasks on close."""
        await self._teardown()

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        """Frame intake. A binary frame → ``4400`` (§6.3); JSON dispatch otherwise.

        Marks client activity for the 90 s socket-silence timer (WS-12); the
        protocol-level ping/pong Channels handles transparently also resets silence
        via this path on data frames.
        """
        self._last_client_activity = time.monotonic()
        if bytes_data is not None:
            # WS-3 / §6.3: binary frames are a protocol violation.
            await self._shutdown(CLOSE_PROTOCOL_VIOLATION)
            return
        await super().receive(text_data=text_data)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        """Dispatch one parsed client frame (``auth`` first, then ``resume``)."""
        if not isinstance(content, dict):
            await self._shutdown(CLOSE_PROTOCOL_VIOLATION)
            return
        msg_type = content.get("type")
        if not self._authed:
            if msg_type != "auth":
                # WS-3 / §6.5 4400: any frame before a valid auth is a violation.
                await self._shutdown(CLOSE_PROTOCOL_VIOLATION)
                return
            await self._handle_auth(content)
            return
        if msg_type == "resume":
            await self._handle_resume(content)
            return
        if msg_type == "auth":
            return  # already authed; a duplicate auth is ignored (idempotent)
        # Unknown post-auth frame type → protocol violation (§6.5 4400).
        await self._shutdown(CLOSE_PROTOCOL_VIOLATION)

    # -- auth (WS-2/WS-3/WS-4/WS-5) ---------------------------------------------

    async def _handle_auth(self, frame: dict[str, Any]) -> None:
        """Resolve the ``auth`` frame, enforce quotas, join the group, send ``ready``.

        On any credential/ownership failure close with the §6.5 code (4401/4403/4404).
        On quota overflow close 4429 (WS-4). On success: parse filters (WS-5), join
        ``stream_{id}``, optionally answer a ``cursor`` with ``resume_ack``/``error``
        (WS-6/WS-8), send ``ready``, and start the sender/heartbeat/revocation tasks.
        """
        result = await database_sync_to_async(resolve_ws_auth)(
            stream_id=self._stream_id, frame=frame
        )
        if result.close_code is not None:
            await self._shutdown(result.close_code)
            return

        assert result.workspace_id is not None
        self._workspace_id = str(result.workspace_id)
        self._key_prefix = result.key_prefix
        self._authed = True
        self._auth_deadline_task.cancel()

        self._parse_filters(frame)
        # The connection's filter-bound fingerprint (WS-5: filters fixed for the
        # socket's life) — every minted cursor binds to (stream, filter set) so the
        # client's REST gap-fill decodes against the same page query (RC-7).
        self._fingerprint = fingerprint_for(
            stream_id=self._stream_id, types=self._types
        )

        # WS-4 connection quota (5/key, 250/workspace → 4429).
        admitted = await database_sync_to_async(self._admit)()
        if not admitted:
            from delivery.domain.ws_protocol import CLOSE_QUOTA_EXCEEDED

            await self._shutdown(CLOSE_QUOTA_EXCEEDED)
            return

        await self.channel_layer.group_add(ws_group_name(self._stream_id), self.channel_name)

        position_cursor = self._tail_cursor()
        await self.send_json(
            build_ready_frame(
                stream_id=self._stream_id,
                cursor=position_cursor,
                types=self._types,
                sample_rate=self._sample_rate,
            )
        )
        self._last_cursor = position_cursor

        cursor = frame.get("cursor")
        if cursor is not None:
            await self._answer_resume(str(cursor))

        self._tasks = [
            asyncio.create_task(self._sender_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]
        if self._key_prefix is not None:
            self._tasks.append(asyncio.create_task(self._revocation_watch()))

    def _parse_filters(self, frame: dict[str, Any]) -> None:
        """Parse ``types`` (≤ 20, WS-5) + ``sample_rate`` ∈ (0,1] from the auth frame."""
        raw_types = frame.get("types")
        if isinstance(raw_types, list):
            cleaned = [str(t) for t in raw_types[:MAX_TYPES_FILTER] if t]
            self._types = tuple(cleaned)
        raw_rate = frame.get("sample_rate")
        if isinstance(raw_rate, (int, float)) and not isinstance(raw_rate, bool):
            rate = float(raw_rate)
            if 0.0 < rate <= 1.0:
                self._sample_rate = rate

    def _admit(self) -> bool:
        from delivery.infra.ws_connections import ConnectionSlot, admit_connection

        assert self._workspace_id is not None
        api_key_id = self._key_prefix  # the per-key set keys on the credential prefix
        slot = ConnectionSlot(
            connection_id=self._connection_id,
            workspace_id=self._workspace_id,
            api_key_id=api_key_id,
        )
        if admit_connection(
            connection_id=self._connection_id,
            workspace_id=self._workspace_id,
            api_key_id=api_key_id,
        ):
            self._slot = slot
            return True
        return False

    def _tail_cursor(self) -> str:
        """The position cursor sent in ``ready`` (the live tail position now).

        The socket never replays (WS-6), so the ``ready`` position is "from here" — a
        synthetic ``latest``-equivalent cursor at the current wall clock with seq 0,
        bound to this connection's filter set. The client's REST bookmark advances
        from each ``event.cursor`` thereafter.
        """
        from delivery.domain.cursor import encode_cursor

        now_ms = int(time.time() * 1000)
        return encode_cursor(p=now_ms, s=0, fingerprint=self._fingerprint)

    # -- resume-from-cursor (WS-6/WS-7/WS-8) ------------------------------------

    async def _handle_resume(self, frame: dict[str, Any]) -> None:
        """A mid-connection ``resume`` re-position request → ``resume_ack`` (§6.4)."""
        cursor = frame.get("cursor")
        if cursor is None:
            await self._shutdown(CLOSE_PROTOCOL_VIOLATION)
            return
        await self._answer_resume(str(cursor))

    async def _answer_resume(self, cursor: str) -> None:
        """Answer a ``cursor`` from ``auth``/``resume`` (WS-6/WS-8).

        The socket NEVER replays (WS-6): the cursor only re-positions the client's
        bookmark. The server replies ``resume_ack`` whose ``behind`` reports the
        approximate gap (``events``, ``from_cursor``) the client REST-fills (WS-7), or
        ``null`` when the cursor is at/ahead of the live tail. An expired cursor does
        not close the socket: reply ``error`` with the §5.4 ``cursor-expired`` problem
        (incl. ``earliest_cursor``) and keep tailing (WS-8).
        """
        outcome = await database_sync_to_async(self._resume_outcome)(cursor)
        if outcome["kind"] == "expired":
            await self.send_json(build_error_frame(problem=outcome["problem"]))
            return
        live = self._tail_cursor()
        await self.send_json(
            build_resume_ack_frame(cursor=live, behind=outcome["behind"])
        )

    def _resume_outcome(self, cursor: str) -> dict[str, Any]:
        """Decode + classify a resume cursor (sync; runs under database_sync_to_async).

        Returns ``{"kind": "expired", "problem": {...}}`` for an expired cursor (WS-8),
        or ``{"kind": "ok", "behind": {...}|None}`` with the approximate gap to the
        live tail (WS-6). Foreign stream / filter mismatch surfaces as a non-fatal
        ``error`` too (kept simple: an undecodable cursor → no behind, the client just
        re-pages). The gap estimate uses the buffer's tail vs. the cursor position.
        """
        from datetime import UTC, datetime

        from delivery.domain.cursor import CursorDecodeError, decode_cursor
        from delivery.infra import buffer_reader
        from tenancy.application.services import worker_workspace_scope

        assert self._workspace_id is not None
        try:
            position = decode_cursor(cursor, expected_fingerprint=self._fingerprint)
        except CursorDecodeError:
            # An undecodable / foreign-bound cursor is non-fatal on the socket: the
            # client simply re-pages REST from its own state (WS-9). No behind gap.
            return {"kind": "ok", "behind": None}

        with worker_workspace_scope(uuid.UUID(self._workspace_id)):
            retention_hours = self._retention_hours()
            now = datetime.now(UTC)
            floor = buffer_reader.retention_floor_ms(
                now=now, retention_hours=retention_hours
            )
            physical = buffer_reader.oldest_partition_floor_ms()
            if buffer_reader.is_expired(
                cursor_p=position.p, retention_floor=floor, physical_floor=physical
            ):
                problem = self._cursor_expired_problem(retention_hours)
                return {"kind": "expired", "problem": problem}
            behind = self._behind_gap(position.p, position.s)
        return {"kind": "ok", "behind": behind}

    def _retention_hours(self) -> int:
        from tenancy.domain.models import WorkspaceQuotas

        quota = WorkspaceQuotas.objects.first()
        return int(quota.buffer_retention_hours) if quota is not None else 24

    def _cursor_expired_problem(self, retention_hours: int) -> dict[str, Any]:
        """The §5.4 ``cursor-expired`` RFC 9457 problem (RC-12; one contract, WS-8)."""
        from delivery.domain.cursor import encode_cursor
        from delivery.infra import buffer_reader

        earliest_p, earliest_s = buffer_reader.earliest_retained_position(
            stream_id=self._stream_id
        )
        earliest = encode_cursor(p=earliest_p, s=earliest_s, fingerprint=self._fingerprint)
        return {
            "type": "https://docs.dataforge.dev/problems/cursor-expired",
            "title": "Cursor expired",
            "status": 410,
            "detail": (
                "This cursor points past the buffer retention window. Resume from "
                "'earliest_cursor', or restart from ?from=earliest."
            ),
            "instance": f"/api/v1/streams/{self._stream_id}/events",
            "earliest_cursor": earliest,
            "retention_hours": retention_hours,
        }

    def _behind_gap(self, cursor_p: int, cursor_s: int) -> dict[str, Any] | None:
        """The approximate ``behind`` gap from the cursor to the live tail (WS-6).

        Counts buffer rows strictly after the cursor (an approximation of how far the
        client is behind); ``None`` when at/ahead of the tail. ``from_cursor`` is the
        client's own cursor — the REST gap-fill start (WS-7).
        """
        from delivery.domain.cursor import encode_cursor
        from delivery.infra import buffer_reader

        page = buffer_reader.read_page(
            stream_id=self._stream_id, p=cursor_p, s=cursor_s, limit=1001
        )
        events = len(page.rows)
        if events == 0:
            return None
        from_cursor = encode_cursor(p=cursor_p, s=cursor_s, fingerprint=self._fingerprint)
        # Cap the reported count at the page probe (1000+); exactness is not required
        # (WS-6 "approximate"); REST gap-fill is the authoritative completeness path.
        return {"events": events if events <= 1000 else 1000, "from_cursor": from_cursor}

    # -- channel-layer fan-in (§6.1; WS-5/WS-10/WS-11) --------------------------

    async def ws_event(self, message: dict[str, Any]) -> None:
        """Handle one ``stream_{id}`` group message from the ws-pusher (§6.1).

        Detects ``frame_seq`` gaps (channel-layer capacity drops, WS-11) → records a
        drop for the next ``drop_notice``; applies the ``types`` filter + ``sample_rate``
        sampling (WS-5) before queueing; enqueues into the 1,000-frame drop-oldest
        queue (WS-10). All non-blocking — never stalls the channel layer or Kafka.
        """
        frame_seq = message.get("frame_seq")
        frame = message.get("frame")
        if not isinstance(frame, dict):
            return
        if isinstance(frame_seq, int):
            self._note_frame_seq(frame_seq)

        event = frame.get("event")
        if isinstance(event, dict) and not self._passes_filters(event):
            return  # filtered/sampled out before queueing (WS-5)

        # Re-mint the cursor bound to THIS connection's filter set so the client's
        # REST gap-fill decodes against its own page query (RC-4: filters narrow,
        # never renumber — the cursor still advances over the unfiltered position).
        cursor = frame.get("cursor")
        if isinstance(event, dict):
            cursor = cursor_after_event(envelope=event, fingerprint=self._fingerprint)
            frame = {"type": "event", "cursor": cursor, "event": event}
        self._queue.put(
            QueuedFrame(frame=frame, resume_cursor=cursor if isinstance(cursor, str) else None)
        )

    def _note_frame_seq(self, frame_seq: int) -> None:
        """Detect a per-stream ``frame_seq`` gap → channel-layer drop (WS-11)."""
        prev = self._last_frame_seq
        self._last_frame_seq = frame_seq
        if prev is not None and frame_seq > prev + 1:
            # A skipped frame_seq is a channel-layer capacity drop: surface it as a
            # drop_notice (INV-DEL-5). The gap size is frame_seq - prev - 1.
            self._dropped += frame_seq - prev - 1
            self._queue_drop_notice(frame_seq - prev - 1, self._last_cursor)

    def _queue_drop_notice(self, dropped: int, resume_cursor: str | None) -> None:
        """Enqueue a ``drop_notice`` frame (channel-layer gap path, WS-11)."""
        if dropped <= 0:
            return
        notice = build_drop_notice_frame(dropped=dropped, resume_cursor=resume_cursor)
        self._queue.put(QueuedFrame(frame=notice, resume_cursor=None))

    def _passes_filters(self, event: dict[str, Any]) -> bool:
        """WS-5: ``types`` exact match (SINK-12) + uniform ``sample_rate`` sampling."""
        if self._types and str(event.get("event_type")) not in self._types:
            return False
        if self._sample_rate < 1.0:
            import random

            # Deliberately unseeded, non-deterministic debug-tail sampling (WS-5).
            if random.random() >= self._sample_rate:
                return False
        return True

    # -- sender / heartbeat / liveness (WS-10/WS-12) ----------------------------

    async def _sender_loop(self) -> None:
        """Drain the send queue to the socket, emitting queued-drop notices (WS-10).

        Before each frame, if the queue dropped oldest frames since the last send, emit
        a ``drop_notice`` with the count + ``resume_cursor`` (the position before the
        gap, for REST gap-fill, INV-DEL-5). Tracks ``delivered`` + the last ``event``
        cursor for the heartbeat counters.
        """
        try:
            while not self._closing:
                item = await self._queue.get()
                if self._queue.has_drops():
                    count, resume_cursor = self._queue.drain_drop_notice()
                    self._dropped += count
                    await self.send_json(
                        build_drop_notice_frame(dropped=count, resume_cursor=resume_cursor)
                    )
                await self.send_json(item.frame)
                if item.frame.get("type") == "event":
                    self._delivered += 1
                    cur = item.frame.get("cursor")
                    if isinstance(cur, str):
                        self._last_cursor = cur
        except asyncio.CancelledError:
            return

    async def _heartbeat_loop(self) -> None:
        """Send a ``heartbeat`` every 15 s + enforce the 90 s silence close (WS-12).

        Each tick also refreshes the connection-quota slot TTL so a live socket never
        expires from the registry (WS-4).
        """
        from datetime import UTC, datetime

        from dataforge_engine.envelope.timestamps import format_rfc3339

        try:
            while not self._closing:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                if time.monotonic() - self._last_client_activity > SILENCE_TIMEOUT_S:
                    from delivery.domain.ws_protocol import CLOSE_GOING_AWAY

                    await self._shutdown(CLOSE_GOING_AWAY)  # WS-12: 90 s silent → 1001
                    return
                if self._slot is not None:
                    await database_sync_to_async(self._refresh_slot)()
                await self.send_json(
                    build_heartbeat_frame(
                        server_time=format_rfc3339(datetime.now(UTC)),
                        last_cursor=self._last_cursor,
                        delivered=self._delivered,
                        dropped=self._dropped,
                    )
                )
        except asyncio.CancelledError:
            return

    def _refresh_slot(self) -> None:
        from delivery.infra.ws_connections import refresh_connection

        if self._slot is not None:
            refresh_connection(self._slot)

    async def _revocation_watch(self) -> None:
        """Kill the live connection < 1 s after the API key is revoked (WS-3).

        Polls the Redis revocation cache for this connection's key prefix every 0.5 s;
        on a revoked verdict closes ``4401`` (ADR-0011). JWT connections have no key
        prefix and skip this watch.
        """
        if self._key_prefix is None:
            return
        try:
            while not self._closing:
                await asyncio.sleep(_REVOCATION_POLL_S)
                revoked = await database_sync_to_async(self._is_key_revoked)()
                if revoked:
                    from delivery.domain.ws_protocol import CLOSE_AUTH_FAILED

                    await self._shutdown(CLOSE_AUTH_FAILED)
                    return
        except asyncio.CancelledError:
            return

    def _is_key_revoked(self) -> bool:
        from tenancy.infra import revocation_cache

        assert self._key_prefix is not None
        return revocation_cache.get_state(self._key_prefix) == revocation_cache.STATE_REVOKED

    # -- shutdown / teardown ----------------------------------------------------

    async def _shutdown(self, code: int) -> None:
        """Close the socket with ``code`` once (idempotent); teardown runs on close."""
        if self._closing:
            return
        self._closing = True
        await self.close(code=code)

    async def _teardown(self) -> None:
        """Cancel background tasks, leave the group, release the quota slot."""
        self._closing = True
        deadline = getattr(self, "_auth_deadline_task", None)
        if deadline is not None:
            deadline.cancel()
        for task in self._tasks:
            task.cancel()
        if self._authed and self._workspace_id is not None:
            from contextlib import suppress

            with suppress(Exception):
                await self.channel_layer.group_discard(
                    ws_group_name(self._stream_id), self.channel_name
                )
        if self._slot is not None:
            from contextlib import suppress

            with suppress(Exception):
                await database_sync_to_async(self._release_slot)()

    def _release_slot(self) -> None:
        from delivery.infra.ws_connections import release_connection

        if self._slot is not None:
            release_connection(self._slot)
