"""ws-pusher sink entrypoint wiring (delivery-channels §6.1; backend-architecture §8.6).

Builds the production ws-pusher :class:`~runner.sinks.host.SinkHost`: a
``confluent_kafka`` consumer in the platform group ``df.sink.websocket.v1`` over
``df.delivery.events.v1`` (the runner's only delivery topic), the
:class:`~delivery.infra.ws_pusher_channel.WsPusherChannel` (strip → ``frame_seq`` →
``group_send``), and the canonical-JSON deserializer shared with the buffer-writer.

Unlike the buffer-writer there is **no tenant arming** (``arm_tenant=None``): the
ws-pusher touches no DB (it fans out to the Redis channel layer), so there is no RLS
to arm. Tenant isolation rides on the auth gate at the per-connection consumer's
group join (WS-3) plus globally unique stream ids in the group name (INV-DEL-6).

The runner supervisor starts this in a worker thread when ``--role`` includes
``sinks`` (the §8.6 consumer is a Kafka group member, not a leased shard — no Redis
lease). The host loop is synchronous (the confluent_kafka consumer is C-driven); the
channel's ``group_send`` is bridged to async inside the channel.

This module is the only place the concrete ws-pusher channel + the concrete consumer
are wired together, keeping the host generic (SINK-10) and the channel broker-agnostic.
"""

from __future__ import annotations

import structlog
from django.conf import settings

from delivery.infra.ws_pusher_channel import WsPusherChannel
from runner.publisher import DELIVERY_TOPIC
from runner.sinks.consumer import WEBSOCKET_GROUP, build_kafka_consumer
from runner.sinks.host import SinkHost
from runner.sinks.run import deserialize_internal

logger = structlog.get_logger("dataforge.runner.sinks.ws")

__all__ = ["build_ws_pusher_host"]


def build_ws_pusher_host(
    *, client_id: str, bootstrap_servers: str | None = None
) -> SinkHost:
    """Construct the production ws-pusher host (consumer + channel + wiring).

    ``client_id`` identifies this member within the ``df.sink.websocket.v1`` group
    (one instance in MVP). The consumer reads ``df.delivery.events.v1`` from the
    earliest retained offset so a fresh group ingests the recent backlog; at-most
    -once means a restart simply re-fans recent frames (idempotent on the socket
    via the client's ``event_id`` dedup).
    """
    servers = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
    consumer = build_kafka_consumer(
        servers, group_id=WEBSOCKET_GROUP, client_id=client_id
    )
    channel = WsPusherChannel()
    host = SinkHost(
        consumer,
        channel,
        topics=[DELIVERY_TOPIC],
        deserialize=deserialize_internal,
        arm_tenant=None,  # no DB write → no RLS to arm (§8.6)
    )
    logger.info(
        "ws_pusher.built",
        group=WEBSOCKET_GROUP,
        topic=DELIVERY_TOPIC,
        client_id=client_id,
    )
    return host
