"""Buffer-writer ``rest_buffer`` sink unit tests (delivery-channels §4).

Covers the buffer-writer's Phase-5 obligations beyond the shared §3.7 conformance
suite (``test_conformance_rest_buffer``):

* **strip → 20-key delivered shape (SB-2 / BW-5).** Every stored ``envelope`` is the
  delivered shape exactly: ``_df`` gone, exactly the 20 contract fields.
* **``acked_through`` only after the DB commit (BW-3 / INV-DEL-3).** The channel
  returns ``acked_through = last_offset`` only on a committed write; a write failure
  returns ``backpressure`` (no ack), so the host commits Kafka offsets *after* the
  insert (offset-after-commit ordering).
* **``buffer_seq`` per-stream monotonic (BW-6).** Sequential batches append a
  strictly increasing, gapless ``buffer_seq`` from the recovered high-water mark.
* **counter recovery (BW-8).** A fresh channel recovers ``max(buffer_seq) + 1`` and
  continues — a redelivery appends duplicates under fresh seqs, never collides.
* **fatal contract (§3.4).** A malformed (unstrippable) envelope → ``fatal_contract``.

SQLite unit lane: ``event_buffer`` is the migration's plain-table fallback; the
COPY path + RLS are exercised on the Postgres lanes (``tests/delivery/test_postgres``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from dataforge_engine.envelope import RESERVED_PREFIX
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import make_batch, make_internal_event
from tests.delivery.test_conformance_rest_buffer import read_buffer_delivered

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _arm(armed_workspace: str) -> Iterator[None]:
    """Arm the engine-fixture workspace for the channel's RLS-scoped writes."""
    yield


def test_strip_yields_exactly_20_delivered_keys(stream_id: str) -> None:
    """Stored rows are the delivered 20-key shape with no ``_df`` (SB-2 / BW-5)."""
    channel = BufferWriterChannel()
    events = [make_internal_event(seq_offset=i) for i in range(4)]
    # The internal input carries _df; assert it is present pre-delivery.
    assert any(k.startswith(RESERVED_PREFIX) for k in events[0])

    result = channel.deliver(make_batch(events))
    assert result.status == "ok"

    for env in read_buffer_delivered(stream_id):
        assert len(env) == 20, "delivered shape is exactly the 20 contract fields"
        assert not any(k.startswith(RESERVED_PREFIX) for k in env)


def test_acked_through_is_last_offset_after_commit(stream_id: str) -> None:
    """``acked_through == last_offset`` only on a committed write (BW-3)."""
    channel = BufferWriterChannel()
    events = [make_internal_event(seq_offset=i) for i in range(3)]
    batch = make_batch(events, first_offset=100)  # offsets 100..102
    result = channel.deliver(batch)
    assert result.status == "ok"
    assert result.acked_through == 102  # the batch's last offset (inclusive)
    assert len(read_buffer_delivered(stream_id)) == 3


def test_offset_after_commit_no_ack_on_write_failure(
    stream_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure returns ``backpressure`` with no ack (offset-after-commit).

    If the DB write raises, the channel MUST NOT ack — the host then does not commit
    the Kafka offset, so the range redelivers (at-least-once, no loss, BW-3/SINK-8).
    """
    from delivery.infra import buffer_store

    channel = BufferWriterChannel()

    def _boom(self: Any, envelopes: Any) -> Any:
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(buffer_store.BufferStore, "write_batch", _boom)
    result = channel.deliver(make_batch([make_internal_event(seq_offset=0)]))
    assert result.status == "backpressure"
    assert result.acked_through is None  # NOT acked → offset not committed
    assert result.retry_after_ms is not None and result.retry_after_ms >= 100


def test_buffer_seq_monotonic_across_batches(stream_id: str) -> None:
    """``buffer_seq`` is strictly increasing + gapless per stream across batches (BW-6)."""
    from delivery.domain.models import EventBuffer

    channel = BufferWriterChannel()
    channel.deliver(make_batch([make_internal_event(seq_offset=i) for i in range(3)]))
    channel.deliver(
        make_batch(
            [make_internal_event(seq_offset=i) for i in range(3, 7)], first_offset=3
        )
    )

    seqs = list(
        EventBuffer.all_objects.filter(stream_id=stream_id)
        .order_by("buffer_seq")
        .values_list("buffer_seq", flat=True)
    )
    assert seqs == [1, 2, 3, 4, 5, 6, 7], "per-stream monotonic gapless buffer_seq"


def test_counter_recovery_continues_after_fresh_channel(stream_id: str) -> None:
    """A fresh channel recovers ``max(buffer_seq)+1`` and continues (BW-8).

    Models a restart/reassignment: the second channel has no in-memory counter and
    must read the high-water mark from the DB, so the combined sequence stays gapless.
    """
    from delivery.domain.models import EventBuffer

    first = BufferWriterChannel()
    first.deliver(make_batch([make_internal_event(seq_offset=i) for i in range(2)]))

    second = BufferWriterChannel()  # cold start — counter recovered from the DB
    second.deliver(
        make_batch(
            [make_internal_event(seq_offset=i) for i in range(2, 5)], first_offset=2
        )
    )

    seqs = list(
        EventBuffer.all_objects.filter(stream_id=stream_id)
        .order_by("buffer_seq")
        .values_list("buffer_seq", flat=True)
    )
    assert seqs == [1, 2, 3, 4, 5]


def test_redelivery_appends_duplicates_under_fresh_seq(stream_id: str) -> None:
    """A redelivered offset range appends duplicates, never collides (BW-3 / BW-8).

    The crash-window redelivery the at-least-once contract licenses: the same events
    re-delivered are re-appended under fresh ``buffer_seq`` (NOT deduped on
    ``event_id``, BW-4).
    """
    channel = BufferWriterChannel()
    events = [make_internal_event(seq_offset=0), make_internal_event(seq_offset=1)]
    channel.deliver(make_batch(events))
    # Redeliver the identical range (crash between DB commit and offset commit).
    BufferWriterChannel().deliver(make_batch(events))

    delivered = read_buffer_delivered(stream_id)
    assert len(delivered) == 4, "redelivery appends duplicate rows (BW-3), never dedupes"


def test_malformed_envelope_is_fatal_contract(stream_id: str) -> None:
    """An unstrippable envelope (missing a delivered field) → ``fatal_contract`` (§3.4)."""
    channel = BufferWriterChannel()
    bad = dict(make_internal_event(seq_offset=0))
    del bad["payload"]  # a required delivered field — strip_internal raises StripError
    result = channel.deliver(make_batch([bad]))  # type: ignore[list-item]
    assert result.status == "fatal"
    assert result.error is not None
    assert result.error.error_class == "fatal_contract"
    assert read_buffer_delivered(stream_id) == [], "nothing persisted on a fatal batch"
