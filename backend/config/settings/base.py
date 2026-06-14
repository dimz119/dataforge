"""Base settings — everything shared, environment-driven (backend-architecture §11).

`dev.py` / `prod.py` / `test.py` override only what genuinely differs.
Selection via `DJANGO_SETTINGS_MODULE`. Env var names and defaults follow
backend-architecture §11 and deployment-architecture §2.3 exactly.
"""

from datetime import timedelta
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
    "rest_framework_simplejwt.token_blacklist",  # SEC-AUTH-9 blacklist storage
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

# INV-ID-1 uses a *partial, case-insensitive* unique index (lower(email) WHERE
# deleted_at IS NULL, database-schema §3.1) instead of a plain unique column —
# soft-deleted accounts may share an email with a live one (the scrub sentinel).
# auth.E003 demands a total unique on USERNAME_FIELD; we satisfy uniqueness via
# the functional index and the boundary normalization, so the check is silenced.
SILENCED_SYSTEM_CHECKS = ["auth.E003"]

# Order is normative (backend-architecture §5.1). The workspace-context
# middleware (Layer 1a) runs after CORS/Common so it owns the fail-closed
# contextvar + GUC lifecycle for the whole request body (security §4.1).
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "observation.api.middleware.RequestIdMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "tenancy.api.middleware.WorkspaceContextMiddleware",
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
# ATOMIC_REQUESTS wraps each request in one transaction so the RLS GUCs set via
# SET LOCAL are transaction-local and die with the request (security §4.1, §9.4).
DATABASES["default"]["ATOMIC_REQUESTS"] = True

# Two-role split (backend-architecture §11, §5.4; SEC-TEN-2): the runtime
# `default` connection carries `dataforge_app` (NOBYPASSRLS, subject to RLS) via
# DATABASE_URL, while DDL/role/maintenance commands connect as the owner
# `dataforge_migrate` via MIGRATE_DATABASE_URL. When the invoked command is one
# of those DDL-class commands (migrate / makemigrations / role provisioning), the
# `default` alias is repointed at the migrate (owner) role for the duration of
# that command so DDL has the privileges it needs — without ever granting the
# runtime role DDL or BYPASSRLS. The runtime processes (runserver, gunicorn,
# uvicorn, celery, runner) keep `dataforge_app`, so RLS bites in production and
# the compose stack (SEC-TEN-2 / Phase 2 exit criterion #2, #6).
#
# The test lane is deliberately NOT routed to the owner: pytest-django connects
# as `dataforge_app` (which holds CREATEDB for the test DB lifecycle) so that the
# raw-SQL RLS probes (§7.3) run under a NOBYPASSRLS role and `FORCE ROW LEVEL
# SECURITY` makes the policies bite for the table owner role too. Connecting the
# probes as a superuser/BYPASSRLS role silently disables RLS and is the exact
# failure this split prevents.
MIGRATE_DATABASE_URL = env.str("MIGRATE_DATABASE_URL", default="")
if MIGRATE_DATABASE_URL:
    import sys as _sys

    # sync_builtin_scenarios writes global (NULL-workspace) catalog + registry
    # rows, which the Class H RLS policies permit only for the maintenance/owner
    # role (database-schema §9.5/§9.6); route it to MIGRATE_DATABASE_URL with the
    # other DDL-class deploy commands so it has the privileges to INSERT them.
    _DDL_COMMANDS = frozenset(
        {
            "migrate",
            "makemigrations",
            "sqlmigrate",
            "showmigrations",
            "provision_db_roles",
            "sync_builtin_scenarios",
        }
    )
    _invoked = _sys.argv[1] if len(_sys.argv) > 1 else ""
    if _invoked in _DDL_COMMANDS:
        _migrate_db = env.db_url("MIGRATE_DATABASE_URL")
        _migrate_db["CONN_MAX_AGE"] = 60
        _migrate_db["ATOMIC_REQUESTS"] = True
        DATABASES["default"] = _migrate_db

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

# --- Email identity (sender + console links, security-architecture §5) -------
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default="no-reply@dataforge.dev")
# Console origin the SPA serves from; verification/reset links point here and the
# refresh endpoint validates Origin against it (SEC-AUTH-4).
CONSOLE_BASE_URL = env.str("CONSOLE_BASE_URL", default="http://localhost:5173")
CONSOLE_ORIGINS: list[str] = env.list("CONSOLE_ORIGINS", default=[CONSOLE_BASE_URL])

# --- Auth token lifetimes (values owned by security-architecture §3.1) -------
JWT_ACCESS_TTL = env.int("JWT_ACCESS_TTL", default=900)  # 15 minutes
JWT_REFRESH_TTL = env.int("JWT_REFRESH_TTL", default=604800)  # 7 days
# Dedicated 256-bit signing key — NEVER DJANGO_SECRET_KEY (security §3.1.2).
# dev default only; prod.py adds it to the required-env manifest.
JWT_SIGNING_KEY = env.str("JWT_SIGNING_KEY", default="dataforge-dev-only-jwt-signing-key")

# --- Password hashing (Argon2id, security-architecture §3.1.1) ---------------
# Argon2PasswordHasher first → all new/upgraded hashes are Argon2id; the rest
# stay so legacy hashes still verify and upgrade-on-login (UPDATE_LAST_LOGIN).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]
# Django's audited Argon2id defaults, restated for review visibility (security §3.1.1):
# time_cost=2, memory_cost=102400 KiB (100 MiB), parallelism=8. The hasher reads
# these from its class attributes; we assert them in tests rather than override.

# Password policy: length 10-128, common-password denylist, user-attribute
# similarity; no composition rules (NIST 800-63B, security §3.1.1).
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]
PASSWORD_MAX_LENGTH = 128  # security §3.1.1 upper bound (enforced at the boundary)

# --- SimpleJWT (frozen defaults, security-architecture §3.1.2) ---------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(seconds=JWT_ACCESS_TTL),
    "REFRESH_TOKEN_LIFETIME": timedelta(seconds=JWT_REFRESH_TTL),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "sub",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
}

# Refresh cookie transport (SEC-AUTH-3). HttpOnly/Secure/SameSite=Strict, path
# scoped to /api/v1/auth, Max-Age 7 d. `Secure` relaxed in dev (no TLS locally).
JWT_REFRESH_COOKIE_NAME = "df_refresh"
JWT_REFRESH_COOKIE_PATH = "/api/v1/auth"
JWT_REFRESH_COOKIE_SAMESITE = "Strict"
JWT_REFRESH_COOKIE_SECURE = env.bool("JWT_REFRESH_COOKIE_SECURE", default=True)

# --- Signup abuse controls (security-architecture §5.4) ----------------------
SIGNUP_DISPOSABLE_EMAIL_BLOCK = env.bool("SIGNUP_DISPOSABLE_EMAIL_BLOCK", default=True)
SIGNUP_CAPTCHA_PROVIDER = env.str("SIGNUP_CAPTCHA_PROVIDER", default="none")  # none|turnstile
TURNSTILE_SECRET_KEY = env.str("TURNSTILE_SECRET_KEY", default="")
# RL-1 signup: 5/h (burst 3) + 20/day per IP (security §5.4). Tunable per-env.
SIGNUP_RATE_LIMIT_PER_HOUR = env.int("SIGNUP_RATE_LIMIT_PER_HOUR", default=5)
SIGNUP_RATE_LIMIT_PER_DAY = env.int("SIGNUP_RATE_LIMIT_PER_DAY", default=20)

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
    # JWT is the console principal (ADR-0011); API-key auth (X-API-Key) is the
    # data-plane principal (security §3.2). Both are default auth classes; a
    # surface that must be JWT-only (identity/tenancy management) sets its own
    # ``authentication_classes`` to exclude the key class (SEC-AUTH-1). A request
    # presenting both headers fails 400 ambiguous-credentials (A-2).
    # API-key auth is listed FIRST so a request presenting BOTH headers fails
    # 400 ambiguous-credentials (A-2) before the JWT class can claim it — DRF
    # stops at the first authenticator that returns non-None, so the ambiguity
    # check must run first. On JWT-only requests the key class returns None and
    # the JWT class runs next.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "tenancy.api.authentication.ApiKeyAuthentication",
        "identity.infra.jwt.DataForgeJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
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

# --- Dataset artifacts (api-spec §4.10; Phase 4) ------------------------------
# Local filesystem path where backfill dataset JSONL artifacts are written before
# download. Phase 11 moves these to object storage with signed-URL redirects; the
# 50,000-event sync/async boundary keeps the synchronous files small.
DATASET_STORAGE_DIR = env.str("DATASET_STORAGE_DIR", default=str(BASE_DIR / "var" / "datasets"))
# The estimate at/below which a dataset is generated synchronously (api §4.10.1).
DATASET_SYNC_EVENT_THRESHOLD = env.int("DATASET_SYNC_EVENT_THRESHOLD", default=50_000)

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
