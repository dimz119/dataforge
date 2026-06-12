"""Base settings — everything shared, environment-driven (backend-architecture §11).

`dev.py` / `prod.py` / `test.py` override only what genuinely differs.
Selection via `DJANGO_SETTINGS_MODULE`. Env var names and defaults follow
backend-architecture §11 and deployment-architecture §2.3 exactly.
"""

from pathlib import Path

import environ

from config.logging import configure_logging

env = environ.Env()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --- Platform identity (backend-architecture §11) ---------------------------
DF_ENV = env.str("DF_ENV", default="dev")  # dev / staging / prod
RELEASE = env.str("DF_RELEASE", default="dev")  # image tag / git SHA (observability §2.2)
# Process role for log `service` field and readyz gating set; each entrypoint
# (wsgi/asgi/celery/runner) sets its own default before settings import.
DF_SERVICE = env.str("DF_SERVICE", default="web")

SECRET_KEY = env.str(
    "DJANGO_SECRET_KEY",
    # dev constant only; prod.py requires the env at boot (backend-architecture §11)
    default="django-insecure-dataforge-dev-only-key",
)

DEBUG = False

ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CORS_ALLOWED_ORIGINS: list[str] = env.list(
    "CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"]
)

# --- Applications ------------------------------------------------------------
# BE-APP-3: Django/3rd-party first, then the ten bounded-context apps in the
# §2.2 table order (`identity` first — it owns AUTH_USER_MODEL).
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "identity",
    "tenancy",
    "catalog",
    "registry",
    "streams",
    "generation",
    "chaos",
    "delivery",
    "observation",
    "audit",
]

AUTH_USER_MODEL = "identity.User"

# Order is normative (backend-architecture §5.1).
# tenancy.api.middleware.WorkspaceContextMiddleware joins in Phase 2.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "observation.api.middleware.RequestIdMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

# --- Stores (deployment-architecture §2.3 dev values) ------------------------
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL", default="postgres://dataforge:dataforge@postgres:5432/dataforge"
    ),
}
DATABASES["default"]["CONN_MAX_AGE"] = 60  # backend-architecture §11

# `dataforge_migrate` role for the release command and maintenance queue
# (backend-architecture §5.4); consumed when those land in later phases.
MIGRATE_DATABASE_URL = env.str("MIGRATE_DATABASE_URL", default="")

REDIS_URL = env.str("REDIS_URL", default="redis://redis:6379/0")
KAFKA_BOOTSTRAP_SERVERS = env.str("KAFKA_BOOTSTRAP_SERVERS", default="kafka:9092")

CELERY_BROKER_URL = REDIS_URL

# --- Email (dev default: Mailpit capture, deployment-architecture §2.3) ------
_email = env.email_url("EMAIL_URL", default="smtp://mailpit:1025")
EMAIL_BACKEND = _email["EMAIL_BACKEND"]
EMAIL_HOST = _email["EMAIL_HOST"]
EMAIL_PORT = _email["EMAIL_PORT"]
EMAIL_HOST_USER = _email["EMAIL_HOST_USER"]
EMAIL_HOST_PASSWORD = _email["EMAIL_HOST_PASSWORD"]

# --- Auth token lifetimes (values owned by security-architecture §3.1) -------
JWT_ACCESS_TTL = env.int("JWT_ACCESS_TTL", default=900)  # 15 minutes
JWT_REFRESH_TTL = env.int("JWT_REFRESH_TTL", default=604800)  # 7 days

# --- Runner / lease contract values (backend-architecture §11) ---------------
RUNNER_TICK_MS = env.int("RUNNER_TICK_MS", default=1000)
RUNNER_SHARD_CAPACITY = env.int("RUNNER_SHARD_CAPACITY", default=8)
RUNNER_EPS_BUDGET = env.int("RUNNER_EPS_BUDGET", default=5000)
LEASE_TTL_MS = env.int("LEASE_TTL_MS", default=15000)
HEARTBEAT_MS = env.int("HEARTBEAT_MS", default=5000)
CHECKPOINT_INTERVAL_MS = env.int("CHECKPOINT_INTERVAL_MS", default=30000)

# --- DRF (backend-architecture §6) -------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_VERSION": "v1",
    "EXCEPTION_HANDLER": "observation.api.problem_details.problem_details_exception_handler",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "DataForge API",
    "DESCRIPTION": "Synthetic data streams for data-engineering education.",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Static assets (prod: WhiteNoise serves the SPA, deployment §8.1) ---------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- I18N / misc --------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Logging (observability §2: structlog owns logging; one chain, all procs) -
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
DF_LOG_LEVELS = env.str("DF_LOG_LEVELS", default="")
LOGGING_CONFIG = None  # Django must not install its default logging config
configure_logging(
    service=DF_SERVICE,
    env_name=DF_ENV,
    release=RELEASE,
    level=LOG_LEVEL,
    per_logger_levels=DF_LOG_LEVELS,
)
