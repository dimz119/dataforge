"""Test settings: local DB, eager Celery tasks, compressed lease/TTL values
(project-folder-structure §2; backend-architecture §11 — env-shaped for test
compression only; production values are contractual).
"""

from config.settings.base import *  # noqa: F403

SECRET_KEY = "test-only-secret-key"

DEBUG = False

# Tests run without live services: in-memory SQLite, in-memory broker, eager tasks.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CELERY_BROKER_URL = "memory://"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Compressed timing values for fast suites.
LEASE_TTL_MS = 1500
HEARTBEAT_MS = 500
CHECKPOINT_INTERVAL_MS = 3000
