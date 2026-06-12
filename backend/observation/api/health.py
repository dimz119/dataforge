"""Platform probes: /healthz and /readyz function views (observability §6).

Plain Django function views, deliberately outside DRF (backend-architecture §6:
plain function views only for /healthz, /readyz, and the WS upgrade path).
"""

from django.conf import settings
from django.http import HttpRequest, JsonResponse

from observation.application import readiness


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness: is this process alive and not wedged?

    No dependency checks — a dead Postgres must not cause restart storms
    (observability §6.1). Serving this request proves the worker loop is
    responsive.
    """
    return JsonResponse({"status": "ok", "service": settings.DF_SERVICE})


def readyz(request: HttpRequest) -> JsonResponse:
    """Readiness: should this process receive work right now?

    Dependency probes with the per-process gating set, 2 s probe timeout,
    5 s result cache; 200 iff all gating components pass (observability §6.1).
    """
    report = readiness.evaluate(settings.DF_SERVICE)
    return JsonResponse(
        {
            "status": "ready" if report.ready else "unready",
            "components": report.components,
            "gating": report.gating,
            "release": settings.RELEASE,
        },
        status=200 if report.ready else 503,
    )
