"""URL routing for the Stream Control API (api-spec §4.8 streams #39-44).

Mounted under /api/v1 by config.urls. Flat collection + single-resource routes
(W-2): the collection route takes ``workspace_id`` from the query/body (JWT) or the
key; the single-resource route resolves the workspace from the stream. Phase 5
mounts create/list/retrieve + the idempotent start/stop verbs; Phase 6 adds the
pause/resume verbs (#45-46, T5/T7) and the live ``PATCH`` mutation (#47, target_tps).
The chaos/delete/events verbs land in their phases.
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
    path(
        "streams/<str:stream_id>/pause",
        viewsets.StreamPauseView.as_view(),
        name="stream-pause",
    ),
    path(
        "streams/<str:stream_id>/resume",
        viewsets.StreamResumeView.as_view(),
        name="stream-resume",
    ),
    path(
        "streams/<str:stream_id>/stats",
        viewsets.StreamStatsView.as_view(),
        name="stream-stats",
    ),
    path(
        "streams/<str:stream_id>/schema-versions",
        viewsets.StreamSchemaVersionsView.as_view(),
        name="stream-schema-versions",
    ),
    path(
        "streams/<str:stream_id>/schema-upgrades",
        viewsets.StreamSchemaUpgradeCollectionView.as_view(),
        name="stream-schema-upgrades",
    ),
    path(
        "streams/<str:stream_id>/schema-upgrades/<str:upgrade_id>",
        viewsets.StreamSchemaUpgradeDetailView.as_view(),
        name="stream-schema-upgrade-detail",
    ),
]
