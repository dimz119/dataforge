"""ASGI entrypoint — uvicorn target for the `ws` process group
(backend-architecture §1: `uvicorn config.asgi`).

Phase 6 replaces this with the Channels ProtocolTypeRouter routing /ws/... to
the delivery consumers (backend-architecture §10).
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DF_SERVICE", "ws")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_asgi_application()
