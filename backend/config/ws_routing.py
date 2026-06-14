"""WebSocket URL routing for the Channels ASGI app (backend-architecture §10).

The WS analogue of ``config.urls``: the per-app WebSocket consumers mounted under
``/ws/...``. Phase 6 ships the live tail (delivery-channels §6): the per-connection
``StreamEventsConsumer`` at ``/ws/streams/{stream_id}/events``. The stream id is a
free-form segment so a malformed id resolves in the consumer's auth gate and masks to
``4404`` (anti-enumeration, WS-3) rather than failing the route.
"""

from __future__ import annotations

from django.urls import path

from delivery.api.consumers import StreamEventsConsumer

websocket_urlpatterns = [
    path(
        "ws/streams/<str:stream_id>/events",
        StreamEventsConsumer.as_asgi(),
        name="ws-stream-events",
    ),
]
