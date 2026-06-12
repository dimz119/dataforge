"""Production settings: security headers and the required-env manifest validated
at boot — a missing required env crashes the process at startup, never at first
use (backend-architecture §11).
"""

import os

from django.core.exceptions import ImproperlyConfigured

from config.settings.base import *  # noqa: F403

DEBUG = False

# --- Required-env manifest (validated at boot) --------------------------------
_REQUIRED_ENVS = (
    "DJANGO_SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "KAFKA_BOOTSTRAP_SERVERS",
    "ALLOWED_HOSTS",
    "EMAIL_URL",
)
_missing = [name for name in _REQUIRED_ENVS if not os.environ.get(name)]
if _missing:
    raise ImproperlyConfigured(
        "missing required environment variables: " + ", ".join(sorted(_missing))
    )

# --- Security headers ----------------------------------------------------------
# TLS termination and HTTPS redirect happen at the Fly edge; health checks reach
# the app over plain HTTP on the private network (deployment-architecture §3.2).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

# --- Static serving: WhiteNoise from the one production image (deployment §8.1)
MIDDLEWARE = MIDDLEWARE.copy()  # noqa: F405
MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
