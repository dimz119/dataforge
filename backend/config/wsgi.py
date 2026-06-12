"""WSGI entrypoint — gunicorn target for the `web` process group
(backend-architecture §1: `gunicorn config.wsgi`).
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DF_SERVICE", "web")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

application = get_wsgi_application()
