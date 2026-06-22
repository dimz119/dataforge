"""Buffer-writer sink entrypoint wiring (delivery-channels §4; backend-architecture §8.6).

Builds the production buffer-writer :class:`~runner.sinks.host.SinkHost`: a
``confluent_kafka`` consumer in the platform group ``df.sink.rest-buffer.v1`` over
``df.delivery.events.v1`` (the runner's only delivery topic), the
:class:`~delivery.infra.buffer_writer_channel.BufferWriterChannel` (strip → COPY →
ack), a canonical-JSON deserializer for the internal record bytes, and the
per-batch tenant-arming context manager (``worker_workspace_scope`` — Layer-1
contextvar + Layer-2 ``app.workspace_id`` GUC, both inside the write transaction so
the buffer rows pass RLS under the NOBYPASSRLS runtime role).

The runner supervisor starts this in a worker thread when ``--role`` includes
``sinks`` (the §8.6 consumer is a Kafka group member, not a leased shard — no Redis
lease). The host loop is synchronous (the confluent_kafka consumer is C-driven);
the thread is cancelled/joined on supervisor shutdown.

This module is the only place the concrete channel + the concrete consumer are
wired together, keeping the host generic (SINK-10) and the channel broker-agnostic.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import structlog
from django.conf import settings

from delivery.infra.buffer_writer_channel import BufferWriterChannel
from runner.publisher import DELIVERY_TOPIC
from runner.sinks.consumer import REST_BUFFER_GROUP, build_kafka_consumer
from runner.sinks.host import SinkHost

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from dataforge_engine.envelope import EnvelopeMapping

logger = structlog.get_logger("dataforge.runner.sinks")

__all__ = ["build_buffer_writer_host", "deserialize_internal"]


def deserialize_internal(raw: bytes) -> EnvelopeMapping:
    """Parse one internal record's canonical-JSON bytes into an envelope mapping.

    The publisher serializes with the S-2 canonical serializer; round-tripping
    through ``json.loads`` yields the internal shape (20 fields + ``_df``). The
    channel ``strip_internal``-s it at ingest (SB-2). Numbers the engine carried as
    ``Decimal`` come back as JSON numbers/strings exactly as serialized — the
    delivered shape preserves the canonical bytes verbatim (envelope round-trip).
    """
    parsed: EnvelopeMapping = json.loads(raw.decode("utf-8"))
    return parsed


def _arm_tenant(workspace_id: uuid.UUID) -> AbstractContextManager[None]:
    """Per-batch RLS arming: contextvar + ``app.workspace_id`` GUC in the write txn.

    Imported lazily so the host stays unit-testable without the tenancy app's
    Django models being loaded by a plain import.
    """
    from tenancy.application.services import worker_workspace_scope

    return worker_workspace_scope(workspace_id)


def build_buffer_writer_host(
    *, client_id: str, bootstrap_servers: str | None = None
) -> SinkHost:
    """Construct the production buffer-writer host (consumer + channel + wiring).

    ``client_id`` identifies this member within the ``df.sink.rest-buffer.v1`` group
    (one instance in MVP, BW-1). The consumer reads ``df.delivery.events.v1`` from
    the earliest retained offset so a fresh group ingests the 24 h backlog.
    """
    servers = bootstrap_servers or settings.KAFKA_BOOTSTRAP_SERVERS
    consumer = build_kafka_consumer(
        servers, group_id=REST_BUFFER_GROUP, client_id=client_id
    )
    channel = BufferWriterChannel()
    host = SinkHost(
        consumer,
        channel,
        topics=[DELIVERY_TOPIC],
        deserialize=deserialize_internal,
        arm_tenant=_arm_tenant,
        group=REST_BUFFER_GROUP,
    )
    logger.info(
        "buffer_writer.built",
        group=REST_BUFFER_GROUP,
        topic=DELIVERY_TOPIC,
        client_id=client_id,
    )
    return host
