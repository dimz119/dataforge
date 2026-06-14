"""The cross-channel ``DeliveryChannel`` conformance suite (delivery-channels §3.7).

Every channel ships with these contract tests; passing them is a phase exit
criterion for the channel's phase (§3.7). The suite is shaped as a reusable mixin
:class:`DeliveryChannelConformance` — a channel's test module subclasses it and
implements the two hooks (build a channel, read back its delivered output) so the
same assertions bind every implementor (rest_buffer now, websocket Phase 6, the
Phase-12 channels later).

The §3.7 rows and where each is proven:

* **Envelope identity** — same input partition slice → byte-equal delivered
  envelopes (content-wise, event-model S-3). Asserted here.
* **Strip scan (SB-3)** — no ``_df``-prefixed key anywhere in delivered output.
  Asserted here; the permanent CI scan extends it per channel.
* **Chaos-duplicate preservation (SINK-4)** — injected duplicates (distinct
  offsets, same ``event_id``) survive; the channel MUST NOT dedupe. Asserted here.
* **Ordering** — per-channel order column of §3.6 holds. Asserted here over the
  delivered sequence.
* **Tenancy (SINK-7)** — a batch whose envelopes disagree with its ``workspace_id``
  is refused ``fatal``. Asserted here.
* **Kill/replay** — SIGKILL the sink host mid-batch → no loss, dupes only as §3.6
  permits. Compose-only (needs a real host + broker); structured for the verify
  agent, not this in-process lane.
* **Backpressure** — a forced sink stall pauses only the affected partition and
  recovers with zero loss. Host-level; covered by the sink-host tests
  (``test_sink_host``) which drive the pause/resume path directly.

These assertions are framework-light: they speak only the ``DeliveryChannel``
contract types and the engine envelope shapes. Concrete channels supply the
durability read-back (the buffer-writer reads ``event_buffer``; a WS channel would
capture frames).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from dataforge_engine.envelope import (
    RESERVED_PREFIX,
    canonical_serialize_str,
    strip_internal,
)
from dataforge_engine.envelope.tests.fixtures import (
    STREAM_ID,
    WORKSPACE_ID,
    order_placed_envelope,
)
from delivery.domain.channel import DeliveryBatch, DeliveryChannel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dataforge_engine.envelope import DeliveredEnvelope, InternalEnvelope


def make_internal_event(*, seq_offset: int, df_canonical: bool = True) -> InternalEnvelope:
    """One internal ``order_placed`` envelope distinguished by ``seq_offset``.

    ``order_placed_envelope`` is deterministic from its ``seed``; varying the seed
    gives a distinct ``event_id`` so a batch carries N distinct delivered instances.
    The ``_df`` block is present (canonical) — the channel strips it at ingest (SB-2).
    """
    env = dict(order_placed_envelope(seed=4242 + seq_offset))
    return env  # type: ignore[return-value]


def make_batch(
    events: Sequence[InternalEnvelope],
    *,
    workspace_id: str = WORKSPACE_ID,
    stream_id: str = STREAM_ID,
    topic: str = "df.delivery.events.v1",
    partition: int = 0,
    first_offset: int = 0,
) -> DeliveryBatch:
    """Wrap internal envelopes in a :class:`DeliveryBatch` in offset order (§3.2)."""
    last = first_offset + len(events) - 1
    return DeliveryBatch(
        workspace_id=UUID(workspace_id),
        stream_id=UUID(stream_id),
        topic=topic,
        partition=partition,
        first_offset=first_offset,
        last_offset=last,
        events=list(events),
    )


def assert_no_reserved_prefix(value: object) -> None:
    """Recursively assert no key begins with the reserved ``_df`` prefix (SB-1/SB-3).

    The permanent strip scan over every channel's delivered output: a hit anywhere
    at any nesting level fails the build.
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            assert not str(key).startswith(RESERVED_PREFIX), (
                f"reserved-prefix key {key!r} survived to delivered output (SB-3)"
            )
            assert_no_reserved_prefix(sub)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_no_reserved_prefix(item)


class DeliveryChannelConformance:
    """Reusable §3.7 conformance assertions for any :class:`DeliveryChannel`.

    A channel's test module subclasses this and implements :meth:`make_channel`
    and :meth:`read_delivered`; the test methods then bind the channel against the
    contract. Subclasses are plain test classes (pytest collects ``test_*``).
    """

    # -- subclass hooks ----------------------------------------------------------

    def make_channel(self) -> DeliveryChannel:
        """Return a fresh, configured channel under test."""
        raise NotImplementedError

    def read_delivered(self, stream_id: str) -> list[DeliveredEnvelope]:
        """Read back, in delivered order, every envelope the channel persisted for
        ``stream_id`` (the buffer-writer reads ``event_buffer`` by ``buffer_seq``).
        """
        raise NotImplementedError

    def deliver(self, channel: DeliveryChannel, batch: DeliveryBatch) -> None:
        """Deliver ``batch`` and assert the channel acked through the last offset."""
        result = channel.deliver(batch)
        assert result.status == "ok", f"expected ok, got {result.status}: {result.error}"
        assert result.acked_through == batch.last_offset

    # -- §3.7 conformance rows ---------------------------------------------------

    def test_envelope_identity(self) -> None:
        """Same input slice → byte-equal delivered envelopes (S-3, §3.7).

        Deliver a batch, read back the delivered output, and assert each row's
        **canonical serialization** equals that of ``strip_internal`` of the
        corresponding input. "Byte-equal" is the S-3 canonical-bytes contract — the
        read-back round-trips through canonical JSON (e.g. ``Decimal`` money renders
        as a string, S-6), so the identity holds at the serialized-bytes level, which
        is exactly the cross-channel property (a consumer migrating REST → Kafka sees
        identical bytes, event-model §5.2).
        """
        channel = self.make_channel()
        events = [make_internal_event(seq_offset=i) for i in range(5)]
        self.deliver(channel, make_batch(events))
        delivered = self.read_delivered(STREAM_ID)
        assert len(delivered) == len(events)
        for got, src in zip(delivered, events, strict=True):
            assert canonical_serialize_str(got) == canonical_serialize_str(
                strip_internal(src)
            )

    def test_strip_scan_no_reserved_prefix(self) -> None:
        """No ``_df``-prefixed key in delivered output, at any nesting (SB-3, §3.7)."""
        channel = self.make_channel()
        events = [make_internal_event(seq_offset=i) for i in range(3)]
        self.deliver(channel, make_batch(events))
        for env in self.read_delivered(STREAM_ID):
            assert_no_reserved_prefix(env)
            assert len(env) == 20, "delivered envelope must be exactly the 20 fields"

    def test_chaos_duplicate_preservation(self) -> None:
        """Distinct-offset duplicates (same ``event_id``) all survive (SINK-4, §3.7).

        Two events with the *same* ``event_id`` but distinct internal offsets are
        chaos duplicates — the channel MUST store both (BW-4: never dedupe on
        ``event_id``; that is the E1 skill being taught).
        """
        channel = self.make_channel()
        original = make_internal_event(seq_offset=0)
        dup = dict(original)  # identical event_id — a chaos duplicate copy
        self.deliver(channel, make_batch([original, dup]))  # type: ignore[list-item]
        delivered = self.read_delivered(STREAM_ID)
        assert len(delivered) == 2, "chaos duplicate must NOT be deduped (BW-4/SINK-4)"
        assert delivered[0]["event_id"] == delivered[1]["event_id"]

    def test_ordering_preserved(self) -> None:
        """Delivered order == input batch order (no reorder, SINK-5/§3.6, §3.7)."""
        channel = self.make_channel()
        events = [make_internal_event(seq_offset=i) for i in range(8)]
        expected_ids = [e["event_id"] for e in events]
        self.deliver(channel, make_batch(events))
        delivered_ids = [env["event_id"] for env in self.read_delivered(STREAM_ID)]
        assert delivered_ids == expected_ids

    def test_tenancy_workspace_mismatch_is_fatal(self) -> None:
        """A batch whose envelopes disagree with its ``workspace_id`` → ``fatal`` (SINK-7)."""
        channel = self.make_channel()
        events = [make_internal_event(seq_offset=0)]
        foreign = "00000000-0000-4000-8000-000000000099"
        batch = make_batch(events, workspace_id=foreign)  # envelope carries the real ws
        result = channel.deliver(batch)
        assert result.status == "fatal"
        assert result.error is not None
        assert result.error.error_class == "fatal_contract"
