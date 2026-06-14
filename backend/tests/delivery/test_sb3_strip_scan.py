"""SB-3 strip-boundary scan — permanent from Phase 5 (testing-strategy §8.2 CON).

> Consume delivered output from every shipped channel and deep-scan every key at
> every nesting level for the ``_df`` prefix; any hit fails. The scan harness is
> channel-parameterized so Phase 12 channels are scanned the day they ship.

This is the *permanent, channel-parameterized* SB-3 gate. The §3.7 conformance
mixin proves each channel strips at its own write; this gate is the standing
cross-channel scan: it enumerates every shipped ``DeliveryChannel`` (Phase 5:
``rest_buffer``) plus the REST cursor *read* surface (the user-facing delivered
output), delivers a batch whose internal envelopes carry a populated ``_df``
control block, and asserts the delivered bytes contain no reserved-prefix key at
any depth AND are exactly the 20-key contract shape.

When Phase 6 (websocket) and Phase 12 (external Kafka, webhooks) ship, they
register here by adding one ``ScannedChannel`` row — zero new assertion logic
(the §8.2 "scanned the day they ship" guarantee). A channel that forgets to strip
fails this gate by construction.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from dataforge_engine.envelope import DELIVERED_FIELD_SET, RESERVED_PREFIX
from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
from delivery.infra.buffer_writer_channel import BufferWriterChannel
from tests.delivery.conformance import assert_no_reserved_prefix, make_batch


@dataclass(frozen=True)
class ScannedChannel:
    """One shipped channel registered for the permanent SB-3 scan.

    ``deliver`` writes a batch through the channel; ``read_delivered`` returns the
    delivered envelopes a consumer would actually see. Phase 6/12 channels add a row.
    """

    name: str
    deliver: Callable[[list[dict[str, Any]], str, str], None]
    read_delivered: Callable[[str], list[dict[str, Any]]]


def _internal_with_df(seq_offset: int, *, workspace_id: str, stream_id: str) -> dict[str, Any]:
    """A canonical internal envelope WITH a populated ``_df`` control block.

    ``order_placed_envelope`` already carries a canonical ``_df`` block — exactly the
    reserved-prefix payload the strip boundary must remove. We feed it through every
    channel; if any leaks, the deep scan catches it.
    """
    env = dict(order_placed_envelope(seed=4242 + seq_offset))
    env["workspace_id"] = workspace_id
    env["stream_id"] = stream_id
    assert any(str(k).startswith(RESERVED_PREFIX) for k in env), (
        "test precondition: the internal envelope must carry a _df block to strip"
    )
    return env


def _rest_buffer_channel(armed_workspace: str, stream_id: str) -> ScannedChannel:
    """The Phase-5 ``rest_buffer`` channel: write via BufferWriterChannel, read via reader."""

    def deliver(events: list[dict[str, Any]], ws_id: str, sid: str) -> None:
        from tenancy.application.services import worker_workspace_scope

        batch = make_batch(events, workspace_id=ws_id, stream_id=sid)  # type: ignore[arg-type]
        with worker_workspace_scope(uuid.UUID(ws_id)):
            result = BufferWriterChannel().deliver(batch)
        assert result.status == "ok", result.error

    def read_delivered(sid: str) -> list[dict[str, Any]]:
        from delivery.infra import buffer_reader
        from tenancy.application.services import worker_workspace_scope

        with worker_workspace_scope(uuid.UUID(armed_workspace)):
            page = buffer_reader.read_page(stream_id=sid, p=0, s=0, limit=1000)
        return [dict(row.envelope) for row in page.rows]

    return ScannedChannel(
        name="rest_buffer", deliver=deliver, read_delivered=read_delivered
    )


# The registry of shipped channels. Phase 6 (websocket) / Phase 12 (kafka, webhook)
# append a builder here — the parametrized test then scans them with zero new logic.
_CHANNEL_BUILDERS: list[Callable[[str, str], ScannedChannel]] = [
    _rest_buffer_channel,
]


@pytest.fixture
def sb3_stream(db: Any) -> Any:
    """A real workspace + stream so the buffer write passes RLS on the Postgres lane."""
    from identity.domain.models import User
    from streams.domain.models import Stream
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    admin = User.objects.create_user(email="sb3-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="SB3 Lab", slug=None)
    ws_context.activate(workspace.id)
    stream_id = str(uuid.uuid4())
    Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name="sb3-stream",
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=datetime.now(UTC),
    )

    class World:
        pass

    world = World()
    world.workspace_id = str(workspace.id)  # type: ignore[attr-defined]
    world.stream_id = stream_id  # type: ignore[attr-defined]
    return world


@pytest.mark.django_db
@pytest.mark.parametrize("builder", _CHANNEL_BUILDERS, ids=lambda b: b.__name__)
def test_sb3_no_reserved_prefix_in_delivered_output(
    builder: Callable[[str, str], ScannedChannel], sb3_stream: Any
) -> None:
    """Every shipped channel's delivered output is _df-free and exactly 20 keys (SB-3).

    Deep-scans every key at every nesting level; any reserved-prefix hit fails. This
    is the permanent gate — a channel that forgets ``strip_internal`` fails here.
    """
    ws_id, sid = sb3_stream.workspace_id, sb3_stream.stream_id
    channel = builder(ws_id, sid)
    events = [
        _internal_with_df(i, workspace_id=ws_id, stream_id=sid) for i in range(4)
    ]
    channel.deliver(events, ws_id, sid)

    delivered = channel.read_delivered(sid)
    assert delivered, f"{channel.name}: no delivered output to scan"
    for env in delivered:
        assert_no_reserved_prefix(env)  # deep, every nesting level
        assert set(env.keys()) == set(DELIVERED_FIELD_SET), (
            f"{channel.name}: delivered envelope is not the 20-key contract shape"
        )
        assert len(env) == 20


def test_sb3_scan_helper_catches_a_leak() -> None:
    """Meta: the deep-scan helper itself fails on a planted reserved-prefix key.

    Guards the gate against a no-op scanner — if ``assert_no_reserved_prefix`` ever
    stopped recursing, this would silently pass and the SB-3 gate would be hollow.
    """
    leaky = {"event_id": "x", "nested": {f"{RESERVED_PREFIX}_secret": 1}}
    with pytest.raises(AssertionError, match="reserved-prefix"):
        assert_no_reserved_prefix(leaky)
