"""Delivery domain layer — the frozen ``DeliveryChannel`` sink contract
(delivery-channels §3) and the ``event_buffer`` model (database-schema §6.1).

Stable downstream import paths:

    from delivery.domain.channel import (
        DeliveryChannel, DeliveryBatch, DeliveryResult,
        SinkError, SinkHealth, ConfigProblem, FlushReason,
        MAX_BATCH_EVENTS, COMMIT_INTERVAL_S, FLUSH_INTERVAL_S,
        clamp_backpressure_ms,
    )
    from delivery.domain.models import EventBuffer
"""

from __future__ import annotations
