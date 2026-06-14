"""Root URLconf.

Mounts the platform probes as plain function views (backend-architecture §6)
and the per-app /api/v1 routers (identity auth/users land in Phase 2; tenancy,
keys, and audit-read mount alongside).
"""

from django.urls import include, path

from catalog.api.urls import urlpatterns as catalog_urlpatterns
from identity.api.urls import urlpatterns as identity_urlpatterns
from observation.api.health import healthz, readyz
from registry.api.urls import urlpatterns as registry_urlpatterns
from tenancy.api.urls import urlpatterns as tenancy_urlpatterns

# /api/v1 surface (URLPathVersioning expects the version segment in the path).
api_v1_patterns = [
    *identity_urlpatterns,
    *tenancy_urlpatterns,
    *catalog_urlpatterns,
    *registry_urlpatterns,
]

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("api/v1/", include((api_v1_patterns, "v1"))),
]
