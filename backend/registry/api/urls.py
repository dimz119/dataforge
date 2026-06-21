"""URL routing for the Schema Registry read API (api-spec §4.12 #62-65).

Mounted under /api/v1 by config.urls. All four endpoints are read-only
(schema-registry §7); there is no registration endpoint in /api/v1. Subject names
contain dots and are used verbatim as path segments — Django's ``str`` converter
matches a dotted segment (anything but ``/``), so a business subject
``{slug}.{event_type}`` and a CDC subject ``{slug}.cdc.{entity}`` both route
correctly.
"""

from django.urls import path

from registry.api import viewsets

urlpatterns = [
    path("schemas", viewsets.SchemaCollectionView.as_view(), name="schemas"),
    # The diff route carries a distinct ``/diff`` suffix (the ``str`` converter
    # matches a dotted subject but stops at ``/``), so it never collides with the
    # detail or versions routes; ``?from=&to=`` are query params, not path segments.
    path(
        "schemas/<str:subject>/diff",
        viewsets.SchemaDiffView.as_view(),
        name="schema-diff",
    ),
    path("schemas/<str:subject>", viewsets.SchemaDetailView.as_view(), name="schema-detail"),
    path(
        "schemas/<str:subject>/versions",
        viewsets.SchemaVersionsView.as_view(),
        name="schema-versions",
    ),
    # The path param is ``schema_version`` (not ``version``) to avoid colliding
    # with the URLPathVersioning ``version`` kwarg drf-spectacular reserves.
    path(
        "schemas/<str:subject>/versions/<str:schema_version>",
        viewsets.SchemaVersionDetailView.as_view(),
        name="schema-version-detail",
    ),
]
