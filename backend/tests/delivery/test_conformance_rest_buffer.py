"""Apply the §3.7 conformance suite to the ``rest_buffer`` buffer-writer channel.

The first :class:`~delivery.domain.channel.DeliveryChannel` implementor (Phase 5):
:class:`~delivery.infra.buffer_writer_channel.BufferWriterChannel`. It must pass
every cross-channel contract row (delivery-channels §3.7) — the executable proof
that the buffer-writer honours the frozen sink contract. The read-back hook reads
``event_buffer`` in ``buffer_seq`` order (the delivered total order, §4.2).

Runs on the SQLite unit lane (no broker, no Postgres): the channel writes through
the Django ``default`` connection and the migration's plain-table fallback backs
``event_buffer``. The kill/replay and backpressure rows (§3.7) are host-level /
compose-only and are covered by ``test_sink_host`` + the verify agent's OPS suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from delivery.domain.channel import DeliveryChannel
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import DeliveryChannelConformance


def read_buffer_delivered(stream_id: str) -> list[dict[str, Any]]:
    """Read every ``event_buffer`` row for ``stream_id`` in ``buffer_seq`` order.

    The delivered envelope is stored as canonical JSON in ``envelope`` (BW-5); the
    page order is ``(partition_ts, buffer_seq)`` == ``buffer_seq`` order (BW-6). We
    return the parsed delivered envelopes, the channel's externally visible output.
    """
    import json

    from delivery.domain.models import EventBuffer

    rows = EventBuffer.all_objects.filter(stream_id=stream_id).order_by(
        "partition_ts", "buffer_seq"
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        env = row.envelope
        out.append(json.loads(env) if isinstance(env, str) else env)
    return out


@pytest.mark.django_db
class TestRestBufferConformance(DeliveryChannelConformance):
    """The ``rest_buffer`` channel against the §3.7 cross-channel contract."""

    @pytest.fixture(autouse=True)
    def _arm(self, armed_workspace: str) -> Iterator[None]:
        """Arm the batch workspace for every conformance method (SINK-7 / RLS).

        ``armed_workspace`` opens the workspace scope + GUC the way the production
        sink entrypoint does per batch; autouse so each inherited ``test_*`` runs
        inside it.
        """
        yield

    def make_channel(self) -> DeliveryChannel:
        return BufferWriterChannel()

    def read_delivered(self, stream_id: str) -> list[dict[str, Any]]:  # type: ignore[override]
        return read_buffer_delivered(stream_id)
