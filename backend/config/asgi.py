"""ASGI entrypoint — uvicorn target for the `ws` process group
(backend-architecture §10: ``uvicorn config.asgi:application``).

Routes ``/ws/...`` to the Channels WebSocket consumers (the live tail,
delivery-channels §6) and everything else to the Django ASGI HTTP app (used only
for protocol completeness — user HTTP goes to the ``web`` WSGI tier). The dedicated
``ws`` group keeps the REST tier stateless and lets WS capacity scale independently
(ADR-0013).

The channel layer (``channels-redis``, settings.CHANNEL_LAYERS) is the fan-out fabric
the ws-pusher sink writes into and the per-connection consumers subscribe to.
"""

import os

os.environ.setdefault("DF_SERVICE", "ws")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

# Django must be set up before importing the routing chain (it loads consumers,
# which reference the app registry).
import django

django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402
from django.core.asgi import get_asgi_application  # noqa: E402

from config.ws_routing import websocket_urlpatterns  # noqa: E402
from observation.infra import metrics  # noqa: E402

_django_asgi_app = get_asgi_application()

# The ws group serves WebSockets, not REST, so its df_ws_* metrics are exposed on a
# side port (DF_METRICS_PORT) like the runner/worker groups (observability §4).
metrics.start_metrics_server(settings.DF_METRICS_PORT)

application = ProtocolTypeRouter(
    {
        "http": _django_asgi_app,
        # AllowedHostsOriginValidator gates the WS handshake Origin against
        # ALLOWED_HOSTS (defense in depth; the first-message auth frame is the real
        # gate, WS-2). URLRouter dispatches /ws/streams/{id}/events to the consumer.
        "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
    }
)
