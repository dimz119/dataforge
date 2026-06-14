"""The ``DeliveryChannel`` sink contract (delivery-channels §3).

Every channel — MVP and future — is an implementation of the :class:`DeliveryChannel`
protocol, hosted by the generic sink host (§3.5). The interface is deliberately
shaped by its most demanding implementor (file-committing object exports), not by
REST/WS: that is what keeps the seam honest (§10). The buffer-writer (Phase 5,
``delivery-channels §4``) is the first implementor; WS (Phase 6) and the Phase-12
channels (external Kafka, webhook, S3/Iceberg) plug into the same harness.

This module is *framework-light* domain code: it imports only the pure engine
(:mod:`dataforge_engine.envelope`) for the envelope/strip shapes, never Django or
``confluent_kafka`` (the latter lives in the runner sink host, ``runner.sinks``).
The host runtime knobs (§3.5) are exported as constants the runner host consumes.

Contract rules SINK-1..SINK-12 (§3.3) and the error classification (§3.4) are the
binding behaviour every implementor must satisfy; the conformance suite
(``tests/delivery/conformance.py``, §3.7) is the executable proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from dataforge_engine.envelope import EnvelopeMapping

__all__ = [
    "COMMIT_INTERVAL_S",
    "COMMIT_MAX_EVENTS",
    "FLUSH_INTERVAL_S",
    "MAX_BACKPRESSURE_MS",
    "MAX_BATCH_BYTES",
    "MAX_BATCH_EVENTS",
    "MIN_BACKPRESSURE_MS",
    "ConfigProblem",
    "DeliveryBatch",
    "DeliveryChannel",
    "DeliveryResult",
    "FlushReason",
    "SinkError",
    "SinkErrorClass",
    "SinkHealth",
    "clamp_backpressure_ms",
]

# -- Sink host runtime knobs (delivery-channels §3.5) ---------------------------
# A sink consumes ≤ 500 events and ≤ 8 MiB serialized per batch, one deliver()
# in flight per (sink instance, topic-partition) (SINK-1).
MAX_BATCH_EVENTS = 500
MAX_BATCH_BYTES = 8 * 1024 * 1024  # 8 MiB serialized per batch

# Offset policy (§3.5): commit only acked_through; cadence 1 s or 1,000 events.
COMMIT_INTERVAL_S = 1.0
COMMIT_MAX_EVENTS = 1000

# Flush interval (§3.5): host calls flush("interval") every 5 s on sinks with
# staged-but-unacked data.
FLUSH_INTERVAL_S = 5.0

# Backpressure clamp (SINK-8): host clamps the sink's retry hint to [100 ms, 60 s].
MIN_BACKPRESSURE_MS = 100
MAX_BACKPRESSURE_MS = 60_000


# The reasons the host invokes flush() (§3.1). "interval" is the periodic flush,
# "rebalance" precedes partition revocation, "shutdown" precedes close().
FlushReason = Literal["interval", "rebalance", "shutdown"]

# Error classes (§3.4). A *retryable* failure is surfaced as backpressure, never
# fatal (SINK-9); *fatal-config* / *fatal-contract* transition the binding to
# ``error``; *poison* applies only to channels with a DLQ (§8.3).
SinkErrorClass = Literal["retryable", "fatal_config", "fatal_contract", "poison"]


@dataclass(frozen=True)
class ConfigProblem:
    """One static-validation problem from :meth:`DeliveryChannel.validate_config`
    (§3.1). An empty list of problems = a valid config. ``field`` points at the
    offending binding-config key; ``code`` is a stable machine token.
    """

    code: str
    message: str
    field: str | None = None


@dataclass(frozen=True)
class SinkError:
    """A classified sink failure carried on a ``fatal`` :class:`DeliveryResult`
    (§3.4). ``error_class`` drives the host/binding effect; ``retryable`` failures
    are expressed as ``backpressure`` instead and never reach here (SINK-9).
    """

    error_class: SinkErrorClass
    message: str
    cause: str | None = None


@dataclass(frozen=True)
class SinkHealth:
    """Cheap liveness signal for ``/readyz`` and sink status (§3.1, ≤ 100 ms).

    ``healthy`` gates readiness; ``detail`` is a free-form diagnostic and
    ``lag_events`` (when known) is the consumer lag the host surfaces (SINK-8).
    """

    healthy: bool
    detail: str = ""
    lag_events: int | None = None


@dataclass(frozen=True)
class DeliveryBatch:
    """One ordered batch from one internal topic-partition (§3.2).

    Events are in offset order with ``_df`` *still present* — the sink calls
    ``strip_internal`` at ingest (SINK-2). ``workspace_id`` is the authoritative
    tenant attribution (SINK-7): a sink refuses (fatal) a batch whose envelopes
    disagree with it. ``first_offset``/``last_offset`` are inclusive.
    """

    workspace_id: UUID
    stream_id: UUID
    topic: str  # internal topic
    partition: int  # internal topic-partition
    first_offset: int  # inclusive
    last_offset: int  # inclusive
    events: Sequence[EnvelopeMapping]  # offset order; _df still present

    @property
    def count(self) -> int:
        return len(self.events)


@dataclass(frozen=True)
class DeliveryResult:
    """The outcome of one :meth:`DeliveryChannel.deliver` / ``flush`` (§3.2).

    ``acked_through`` is the durability cursor decoupled from batch boundaries
    (SINK-3): the highest internal-Kafka offset (inclusive) that is *durably*
    delivered, or ``None`` for no new durability. The host commits internal
    consumer-group offsets only up to ``acked_through``; everything above is
    redelivered after a crash (the at-least-once budget). ``retry_after_ms`` is
    set on ``backpressure`` only (host clamps + backoffs, SINK-8); ``error`` is
    set on ``fatal`` only (§3.4).
    """

    status: Literal["ok", "backpressure", "fatal"]
    acked_through: int | None = None
    retry_after_ms: int | None = None
    error: SinkError | None = None

    @classmethod
    def ok(cls, *, acked_through: int | None) -> DeliveryResult:
        """An ``ok`` result acking through ``acked_through`` (SINK-3)."""
        return cls(status="ok", acked_through=acked_through)

    @classmethod
    def backpressure(
        cls, *, retry_after_ms: int, acked_through: int | None = None
    ) -> DeliveryResult:
        """A ``backpressure`` result (SINK-8); the host pauses the partition."""
        return cls(
            status="backpressure",
            acked_through=acked_through,
            retry_after_ms=retry_after_ms,
        )

    @classmethod
    def fatal(cls, error: SinkError) -> DeliveryResult:
        """A ``fatal`` result (§3.4) — the binding transitions to ``error``."""
        return cls(status="fatal", acked_through=None, error=error)


def clamp_backpressure_ms(hint_ms: int) -> int:
    """Clamp a sink's backpressure hint to ``[100 ms, 60 s]`` (SINK-8).

    The host owns the exponential growth (x2) + jitter + reset-on-``ok``; this
    helper is the pure clamp shared by the host and the conformance suite so the
    bound is one definition.
    """
    return max(MIN_BACKPRESSURE_MS, min(MAX_BACKPRESSURE_MS, hint_ms))


@runtime_checkable
class DeliveryChannel(Protocol):
    """The frozen sink contract (delivery-channels §3.1).

    A channel is hosted by the generic sink host (§3.5): the host owns the Kafka
    consumer group, batching, offset commits (up to ``acked_through`` only),
    rebalance/flush cadence, and backpressure clamping; the channel owns the
    durability of one batch. Implementors: ``rest_buffer`` (Phase 5), ``websocket``
    (Phase 6), ``kafka_external``/``webhook``/``object_export`` (Phase 12).

    Contract rules SINK-1..SINK-12 (§3.3) and the error classification (§3.4) bind
    every implementor; the conformance suite (§3.7) is the executable proof.
    """

    # "rest_buffer" | "websocket" | "kafka_external" | "webhook" | "object_export"
    channel_type: ClassVar[str]

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> list[ConfigProblem]:
        """Static validation at SinkBinding create/update (control plane, §3.1).

        Includes live probes where the config names external resources (S3 probe
        write, Iceberg catalog ping, webhook URL policy check). Empty list = valid.
        """
        ...

    def configure(self, binding: Any, secrets: Any) -> None:
        """Instantiate at sink-host start / partition assignment (§3.1).

        Must be side-effect-free beyond connection setup; provisioning (topic
        creation, ACLs) happens in the control plane, not here. ``binding`` is the
        ``SinkBinding`` and ``secrets`` the ``SecretBundle`` (typed in the channel's
        own app; ``Any`` here keeps the platform-shared interface free of the
        per-channel control-plane shapes).
        """
        ...

    def deliver(self, batch: DeliveryBatch) -> DeliveryResult:
        """Deliver one ordered batch from one internal topic-partition (§3.1).

        Must call ``strip_internal()`` on every envelope before any external
        persistence or serialization (event-model SB-2). Returns the durability
        cursor in ``acked_through`` (SINK-3); ``backpressure``/``fatal`` per §3.4.
        """
        ...

    def flush(self, reason: FlushReason) -> DeliveryResult:
        """Force sink-internal staging to durability (§3.1).

        After ``flush`` returns ``ok``, everything previously accepted by
        ``deliver`` is durable and reflected in ``acked_through`` (SINK-6). Called
        on shutdown, partition revocation, and the host's flush interval.
        """
        ...

    def healthcheck(self) -> SinkHealth:
        """Cheap liveness signal for ``/readyz`` and sink status (§3.1, ≤ 100 ms)."""
        ...

    def close(self) -> None:
        """Release connections (§3.1). The host guarantees ``flush`` completed
        first."""
        ...
