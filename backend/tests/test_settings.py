"""Settings-split smoke tests (backend-architecture §11) — no live services."""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

_PROD_REQUIRED = {
    "DJANGO_SECRET_KEY": "prod-secret",
    # Dedicated JWT signing key, never DJANGO_SECRET_KEY (security §3.1.2).
    "JWT_SIGNING_KEY": "prod-jwt-signing-key",
    "DATABASE_URL": "postgres://dataforge:dataforge@postgres:5432/dataforge",
    "REDIS_URL": "redis://redis:6379/0",
    "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
    "ALLOWED_HOSTS": "api.dataforge.dev",
    "EMAIL_URL": "smtp://mail.example.com:587",
    "CONSOLE_BASE_URL": "https://app.dataforge.dev",
}


def _fresh_import(module: str) -> object:
    sys.modules.pop(module, None)
    return importlib.import_module(module)


@pytest.mark.parametrize(
    "module", ["config.settings.base", "config.settings.dev", "config.settings.test"]
)
def test_settings_modules_import(module: str) -> None:
    settings = _fresh_import(module)
    assert settings.AUTH_USER_MODEL == "identity.User"  # type: ignore[attr-defined]


def test_installed_apps_order_is_be_app_3(settings: object) -> None:
    """The ten bounded-context apps, identity first (backend-architecture BE-APP-3)."""
    from django.conf import settings as django_settings

    apps = django_settings.INSTALLED_APPS
    expected = [
        "identity", "tenancy", "catalog", "registry", "streams",
        "generation", "chaos", "delivery", "observation", "audit",
    ]
    assert [app for app in apps if app in expected] == expected


def test_prod_settings_import_with_required_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _PROD_REQUIRED.items():
        monkeypatch.setenv(key, value)
    settings = _fresh_import("config.settings.prod")
    assert settings.DEBUG is False  # type: ignore[attr-defined]
    sys.modules.pop("config.settings.prod", None)


def test_prod_settings_crash_at_boot_on_missing_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing required env crashes the process at startup, never at first use (§11)."""
    for key in _PROD_REQUIRED:
        monkeypatch.delenv(key, raising=False)
    sys.modules.pop("config.settings.prod", None)
    with pytest.raises(ImproperlyConfigured):
        importlib.import_module("config.settings.prod")
    sys.modules.pop("config.settings.prod", None)
