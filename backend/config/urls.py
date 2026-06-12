"""Root URLconf.

Mounts the platform probes as plain function views (backend-architecture §6)
and, as endpoints land in later phases, the per-app /api/v1 routers.
"""

from django.urls import path

from observation.api.health import healthz, readyz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
]
