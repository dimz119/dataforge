"""The per-connection drop-oldest send queue (delivery-channels §6.5 WS-10).

A bounded asyncio queue between the channel-layer fan-in (the consumer's ``ws_event``
handler) and the socket sender task. WS-10: the per-connection send queue caps at
**1,000 frames**; on overflow the server drops the **oldest** queued frame and
records it so the consumer can emit a ``drop_notice`` with the count and a
``resume_cursor`` (the position before the gap, for REST gap-fill — INV-DEL-5).
Dropping never blocks the channel layer or Kafka — a slow socket hurts only itself
(the tail stays live; completeness is REST's job).

This is the *queue mechanics* only — pure asyncio + a frozen item type — so it is
unit-testable without a socket. The consumer owns the sender task that drains it and
the ``drop_notice`` emission; the queue records (a) how many oldest frames were
dropped since the last drain and (b) the ``resume_cursor`` carried by the OLDEST
surviving frame after a drop (the position before the gap the client must REST-fill).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any

from delivery.domain.ws_protocol import SEND_QUEUE_CAP

__all__ = ["DropOldestSendQueue", "QueuedFrame"]


@dataclass(frozen=True)
class QueuedFrame:
    """One frame awaiting send, with the REST resume cursor it sits before/at.

    ``frame`` is the S→C JSON document (an ``event``/``resume_ack``/etc. shape).
    ``resume_cursor`` is the REST-interchangeable position the client would resume
    from if this frame and everything after the drop point were lost — for ``event``
    frames it is the frame's own ``cursor`` (the position before the gap, WS-10); for
    control frames it is ``None`` (they carry no position).
    """

    frame: dict[str, Any]
    resume_cursor: str | None


class DropOldestSendQueue:
    """A bounded FIFO that drops the oldest frame on overflow (WS-10).

    ``put`` never blocks: at capacity it pops the oldest frame, increments the
    pending-drop counter, and remembers that dropped frame's ``resume_cursor`` as the
    gap's lower bound. ``drain_drop_notice`` returns ``(dropped_count, resume_cursor)``
    and resets the counter — the consumer calls it to mint a ``drop_notice`` (the
    ``resume_cursor`` is the position before the gap so the client REST-fills exactly
    the dropped range, INV-DEL-5).
    """

    def __init__(self, *, capacity: int = SEND_QUEUE_CAP) -> None:
        self._capacity = capacity
        self._items: deque[QueuedFrame] = deque()
        self._dropped_since_notice = 0
        self._drop_resume_cursor: str | None = None
        self._available = asyncio.Event()

    def put(self, item: QueuedFrame) -> None:
        """Enqueue ``item``, dropping the oldest frame if at capacity (WS-10)."""
        if len(self._items) >= self._capacity:
            oldest = self._items.popleft()
            self._dropped_since_notice += 1
            # The gap begins at the OLDEST dropped frame's position: the client must
            # REST-fill from there. Keep the earliest dropped cursor across a burst.
            if oldest.resume_cursor is not None and self._drop_resume_cursor is None:
                self._drop_resume_cursor = oldest.resume_cursor
        self._items.append(item)
        self._available.set()

    async def get(self) -> QueuedFrame:
        """Pop the oldest queued frame, awaiting one if the queue is empty."""
        while not self._items:
            self._available.clear()
            await self._available.wait()
        item = self._items.popleft()
        if not self._items:
            self._available.clear()
        return item

    def has_drops(self) -> bool:
        """True iff frames were dropped since the last :meth:`drain_drop_notice`."""
        return self._dropped_since_notice > 0

    def drain_drop_notice(self) -> tuple[int, str | None]:
        """Return ``(dropped_count, resume_cursor)`` and reset the drop state (WS-10).

        ``resume_cursor`` is the position before the gap (the earliest dropped frame's
        cursor) for REST gap-fill; ``None`` if no positional frame was among the drops.
        """
        count = self._dropped_since_notice
        cursor = self._drop_resume_cursor
        self._dropped_since_notice = 0
        self._drop_resume_cursor = None
        return count, cursor

    def __len__(self) -> int:
        return len(self._items)
