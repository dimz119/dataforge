"""XCH-1/2 cross-channel content harness (testing-strategy §8.3; phase-06 exit #4).

The Phase-6 exit criterion "WS and REST deliver the same stream content," proven at
the *channel seam* without a live broker: drive the SAME source envelopes through
both delivery channels — the ws-pusher fan-out (:class:`WsPusherChannel`, the WS
event frames) and the REST-delivered shape (``strip_internal``, exactly what the
buffer-writer COPYs into ``event_buffer`` and the cursor pull returns) — then
reconcile per ``event_id`` with the pure cross-channel harness
(:func:`tests.ops.stream_control_harness.compare_channels` / ``assert_xch``).

This gates the content-equality + drop-reconciliation LOGIC on the PR lane against
the real channel objects (both strip ``_df`` at ingest, both render the delivered
20-key shape, S-3 wire-order independence). The LIVE 60-second harness — a WS tail
consumer + a REST cursor consumer over a running Kafka stream — is the compose-only
variant the verify agent runs (demo-phase06.sh step 10); its assertions are these
same functions, so a regression here fails the PR before the compose run.

XCH-1 (clean): identical event_id sets, content-equal, zero drops.
XCH-2 (chaos): WS at-most-once subset, drop-notice counts reconcile the gap exactly
(INV-DEL-5) — modelled here by dropping a deterministic subset from the WS side and
asserting the reported drop count reconciles.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from dataforge_engine.envelope import strip_internal
from dataforge_engine.envelope.tests.fixtures import (
    STREAM_ID,
    WORKSPACE_ID,
    order_placed_envelope,
)
from delivery.domain.channel import DeliveryBatch
from delivery.infra.ws_pusher_channel import ChannelLayerSender, WsPusherChannel
from tests.ops import stream_control_harness as h


class _CapturingLayer:
    """Records every ws-pusher ``group_send`` so we can read back the WS event frames."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def group_send(self, group: str, message: dict[str, Any]) -> None:
        self.sent.append(message)


def _distinct_envelopes(count: int) -> list[dict[str, Any]]:
    """A window of internal envelopes with distinct event_id / sequence_no / payload.

    The shared fixture is one canonical order_placed; vary the identity-bearing keys
    so the per-event content comparison is meaningful (distinct ids, ordered seqs)."""
    out: list[dict[str, Any]] = []
    for i in range(count):
        env: dict[str, Any] = dict(order_placed_envelope())
        env["event_id"] = f"019b7700-0000-7000-8000-{i:012d}"
        env["sequence_no"] = i + 1
        payload: dict[str, Any] = dict(env["payload"])
        payload["order_id"] = f"ord_{i:016d}"
        env["payload"] = payload
        out.append(env)
    return out


def _ws_frame_events(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fan a window through the ws-pusher and return the delivered WS ``event`` dicts."""
    layer = _CapturingLayer()
    channel = WsPusherChannel(sender=ChannelLayerSender(layer))
    batch = DeliveryBatch(
        workspace_id=UUID(WORKSPACE_ID),
        stream_id=UUID(STREAM_ID),
        topic="df.delivery.events.v1",
        partition=0,
        first_offset=0,
        last_offset=len(envelopes) - 1,
        events=envelopes,
    )
    result = channel.deliver(batch)
    assert result.status == "ok"
    return [m["frame"]["event"] for m in layer.sent]


def _rest_delivered(envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The REST-delivered shape: ``strip_internal`` (what the buffer-writer COPYs and
    the cursor pull returns) — the same SB-2 strip the ws-pusher applies."""
    return [dict(strip_internal(env)) for env in envelopes]


def test_xch1_clean_identical_content_across_channels() -> None:
    """XCH-1: WS and REST deliver identical event_id sets + per-event content."""
    window = _distinct_envelopes(50)
    rest = _rest_delivered(window)
    ws = _ws_frame_events(window)

    report = h.compare_channels(rest, ws)
    assert report.rest_ids == report.ws_ids  # identical sets at a clean rate
    h.assert_xch(report, ws_drop_notice_total=0, clean=True)


def test_xch_both_channels_strip_internal_df() -> None:
    """Neither channel delivers a ``_df`` key (SB-3) — the cross-channel strip parity."""
    window = _distinct_envelopes(10)
    for event in _ws_frame_events(window) + _rest_delivered(window):
        assert not any(k.startswith("_df") for k in event)
        assert len(event) == 20  # the delivered field set, both channels


def test_xch2_chaos_subset_reconciles_via_drop_notice() -> None:
    """XCH-2: WS at-most-once subset; the drop-notice count reconciles the gap exactly.

    Model the backpressure outcome: REST has the full window, the WS tail dropped a
    deterministic subset, and the connection's drop notices reported exactly that many
    (INV-DEL-5). The content of every commonly-delivered event is still identical."""
    window = _distinct_envelopes(50)
    rest = _rest_delivered(window)
    full_ws = _ws_frame_events(window)
    # The WS connection dropped 7 frames under backpressure (every 7th), and emitted
    # drop notices summing to exactly 7 — the live consumer's drop_notice accounting.
    dropped_ids = {full_ws[i]["event_id"] for i in range(0, len(full_ws), 7)}
    ws_subset = [e for e in full_ws if e["event_id"] not in dropped_ids]

    report = h.compare_channels(rest, ws_subset)
    assert report.rest_only == dropped_ids  # the gap is exactly the dropped set
    h.assert_xch(report, ws_drop_notice_total=len(dropped_ids), clean=False)


def test_xch2_chaos_fails_when_drop_notice_underreports() -> None:
    """A dropped-but-unreported frame fails reconciliation (the silent-loss guard)."""
    window = _distinct_envelopes(30)
    rest = _rest_delivered(window)
    full_ws = _ws_frame_events(window)
    ws_subset = full_ws[:-3]  # 3 dropped, but the connection reports only 1

    report = h.compare_channels(rest, ws_subset)
    with pytest.raises(AssertionError, match="reconcile exactly"):
        h.assert_xch(report, ws_drop_notice_total=1, clean=False)
