"""URL routing for the Generation API — the datasets surface (api-spec §4.10).

Mounted under /api/v1 by config.urls. Routes #57-61 (datasets collection, detail,
download). The dataset id is the resource discriminator (not a workspace id), so a
foreign id masks to 404 via the scoped manager.
"""

from django.urls import path

from generation.api.viewsets import (
    DatasetCollectionView,
    DatasetDetailView,
    DatasetDownloadView,
)

urlpatterns = [
    path("datasets", DatasetCollectionView.as_view(), name="datasets-collection"),
    path(
        "datasets/<str:dataset_id>/download",
        DatasetDownloadView.as_view(),
        name="datasets-download",
    ),
    path("datasets/<str:dataset_id>", DatasetDetailView.as_view(), name="datasets-detail"),
]
