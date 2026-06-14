"""URL routing for the Stream Control API (api-spec §4.8 streams #39-44).

Mounted under /api/v1 by config.urls. Flat collection + single-resource routes
(W-2): the collection route takes ``workspace_id`` from the query/body (JWT) or the
key; the single-resource route resolves the workspace from the stream. Phase 5
mounts create/list/retrieve + the idempotent start/stop verbs; the remaining verbs
(pause/resume/PATCH/chaos/delete/events) land in their phases.
"""

from django.urls import path

from streams.api import viewsets

urlpatterns = [
    path("streams", viewsets.StreamCollectionView.as_view(), name="streams"),
    path("streams/<str:stream_id>", viewsets.StreamDetailView.as_view(), name="stream-detail"),
    path(
        "streams/<str:stream_id>/start",
        viewsets.StreamStartView.as_view(),
        name="stream-start",
    ),
    path(
        "streams/<str:stream_id>/stop",
        viewsets.StreamStopView.as_view(),
        name="stream-stop",
    ),
]
