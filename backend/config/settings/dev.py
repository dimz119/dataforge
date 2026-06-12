"""Development settings: DEBUG, relaxed CORS, Mailpit SMTP capture
(backend-architecture §11; deployment-architecture §2.1).
"""

from config.settings.base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ["*"]

# Relaxed CORS for the Vite dev server and local tooling.
CORS_ALLOW_ALL_ORIGINS = True

# Email already defaults to the Mailpit capture container via EMAIL_URL
# (smtp://mailpit:1025, deployment-architecture §2.3).
