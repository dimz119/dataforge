"""URL routing for the Scenario Catalog API (api-spec §4.6 #26-32, §4.7 #33-38).

Mounted under /api/v1 by config.urls. The scenario surface (#26-32) is not
workspace-path-scoped (globals readable by all; the caller's own workspace opted
in via ?workspace_id or the key's workspace). Scenario instances (#33-38) live
under /workspaces/{workspace_id}/scenario-instances (Class-T tenant rows).
"""

from django.urls import path

from catalog.api import viewsets

urlpatterns = [
    # Scenarios (#26-32).
    path("scenarios", viewsets.ScenarioCollectionView.as_view(), name="scenarios"),
    path(
        "scenarios/<str:scenario_slug>",
        viewsets.ScenarioDetailView.as_view(),
        name="scenario-detail",
    ),
    path(
        "scenarios/<str:scenario_slug>/versions",
        viewsets.ScenarioVersionsView.as_view(),
        name="scenario-versions",
    ),
    path(
        "scenarios/<str:scenario_slug>/versions/<str:manifest_version>",
        viewsets.ScenarioVersionDetailView.as_view(),
        name="scenario-version-detail",
    ),
    path(
        "scenarios/<str:scenario_slug>/versions/<str:manifest_version>/validation",
        viewsets.ScenarioVersionValidationView.as_view(),
        name="scenario-version-validation",
    ),
    path(
        "scenarios/<str:scenario_slug>/versions/<str:manifest_version>/publish",
        viewsets.ScenarioPublishView.as_view(),
        name="scenario-version-publish",
    ),
    # Scenario instances (#33-38).
    path(
        "workspaces/<str:workspace_id>/scenario-instances",
        viewsets.ScenarioInstanceCollectionView.as_view(),
        name="scenario-instances",
    ),
    path(
        "workspaces/<str:workspace_id>/scenario-instances/<str:scenario_instance_id>",
        viewsets.ScenarioInstanceDetailView.as_view(),
        name="scenario-instance-detail",
    ),
    path(
        "workspaces/<str:workspace_id>/scenario-instances/<str:scenario_instance_id>/configuration",
        viewsets.ScenarioInstanceConfigurationView.as_view(),
        name="scenario-instance-configuration",
    ),
]
