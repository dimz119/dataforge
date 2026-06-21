"""URL routing for the Chaos API (api-spec §4.8.3 chaos + §4.13 answer-key).

Mounted under /api/v1 by config.urls. Flat single-resource routes under a stream
id (W-2): the chaos policy GET|PATCH and the three answer-key reads. Each resolves
the owning workspace from the stream and masks foreign access to 404.
"""

from django.urls import path

from chaos.api import viewsets

urlpatterns = [
    path(
        "streams/<str:stream_id>/chaos",
        viewsets.StreamChaosView.as_view(),
        name="stream-chaos",
    ),
    path(
        "streams/<str:stream_id>/answer-key/injections",
        viewsets.AnswerKeyInjectionsView.as_view(),
        name="stream-answer-key-injections",
    ),
    path(
        "streams/<str:stream_id>/answer-key/summary",
        viewsets.AnswerKeySummaryView.as_view(),
        name="stream-answer-key-summary",
    ),
    path(
        "streams/<str:stream_id>/answer-key/export",
        viewsets.AnswerKeyExportView.as_view(),
        name="stream-answer-key-export",
    ),
]
