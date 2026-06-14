"""URL routing for the Delivery API.

Mounted under /api/v1 by config.urls. Phase 5 ships the REST cursor pull
(delivery-channels §5; api-spec §4.9.1): ``GET /streams/{id}/events``. The route
lives in the delivery app (it reads ``event_buffer``, the delivery context) even
though the path is nested under ``/streams`` — the stream-id is just the page-query
discriminator. WS resume (§6) lands in Phase 6.
"""

from django.urls import path

from delivery.api import viewsets

urlpatterns = [
    path(
        "streams/<str:stream_id>/events",
        viewsets.StreamEventsView.as_view(),
        name="stream-events",
    ),
]
