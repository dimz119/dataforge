"""Signup abuse controls: rate limiter, disposable-email denylist, captcha (§5.4)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from django.test import override_settings

from identity.infra import captcha, disposable_email, rate_limit


# --- Disposable-email denylist (SEC-ACC-9) -----------------------------------
def test_disposable_email_detects_denylisted_domain() -> None:
    assert disposable_email.is_disposable_email("bot@mailinator.com") is True
    assert disposable_email.is_disposable_email("bot@GuerrillaMail.com") is True  # case-insensitive


def test_disposable_email_allows_normal_domain() -> None:
    assert disposable_email.is_disposable_email("ada@example.com") is False


# --- Captcha hook (SEC-ACC-10) -----------------------------------------------
@override_settings(SIGNUP_CAPTCHA_PROVIDER="none")
def test_captcha_none_always_passes() -> None:
    assert captcha.captcha_required() is False
    captcha.verify_captcha(None)  # no raise


@override_settings(SIGNUP_CAPTCHA_PROVIDER="turnstile", TURNSTILE_SECRET_KEY="secret")
def test_captcha_turnstile_required_and_rejects_missing_token() -> None:
    assert captcha.captcha_required() is True
    with pytest.raises(captcha.CaptchaError):
        captcha.verify_captcha(None)


@override_settings(SIGNUP_CAPTCHA_PROVIDER="turnstile", TURNSTILE_SECRET_KEY="secret")
def test_captcha_turnstile_success_path() -> None:
    fake = mock.MagicMock()
    fake.read.return_value = b'{"success": true}'
    fake.__enter__.return_value = fake
    with mock.patch("identity.infra.captcha.urllib.request.urlopen", return_value=fake):
        captcha.verify_captcha("good-token")  # no raise


@override_settings(SIGNUP_CAPTCHA_PROVIDER="turnstile", TURNSTILE_SECRET_KEY="secret")
def test_captcha_turnstile_fail_closed_on_network_error() -> None:
    import urllib.error

    with mock.patch(
        "identity.infra.captcha.urllib.request.urlopen",
        side_effect=urllib.error.URLError("down"),
    ):
        with pytest.raises(captcha.CaptchaError):
            captcha.verify_captcha("token")


# --- Rate limiter (RL-1) -----------------------------------------------------
class _FakePipe:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._ops: list[tuple[str, str]] = []

    def incr(self, key: str) -> None:
        self._ops.append(("incr", key))

    def expire(self, key: str, _seconds: int, nx: bool = False) -> None:
        self._ops.append(("expire", key))

    def execute(self) -> None:
        for op, key in self._ops:
            if op == "incr":
                self._store[key] = self._store.get(key, 0) + 1


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def mget(self, keys: list[str]) -> list[int | None]:
        return [self.store.get(k) for k in keys]

    def ttl(self, _key: str) -> int:
        return 1800

    def pipeline(self) -> _FakePipe:
        return _FakePipe(self.store)


def test_rate_limit_allows_under_limit_then_denies(monkeypatch: Any) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "_redis", lambda: fake)
    windows = (rate_limit.Window(limit=3, seconds=3600),)
    for _ in range(3):
        assert rate_limit.check("signup", "1.2.3.4", windows).allowed is True
    # 4th call: window exhausted.
    result = rate_limit.check("signup", "1.2.3.4", windows)
    assert result.allowed is False
    assert result.retry_after == 1800


def test_rate_limit_fails_open_when_redis_unavailable(monkeypatch: Any) -> None:
    import redis as redis_lib

    def _boom() -> Any:
        raise redis_lib.RedisError("down")

    monkeypatch.setattr(rate_limit, "_redis", _boom)
    windows = (rate_limit.Window(limit=1, seconds=3600),)
    # Degraded limiter must not lock out legitimate signups.
    assert rate_limit.check("signup", "9.9.9.9", windows).allowed is True


def test_client_ip_prefers_fly_header() -> None:
    from django.test import RequestFactory

    req = RequestFactory().post("/", HTTP_FLY_CLIENT_IP="203.0.113.7", REMOTE_ADDR="10.0.0.1")
    assert rate_limit.client_ip(req) == "203.0.113.7"


def test_client_ip_does_not_trust_x_forwarded_for() -> None:
    from django.test import RequestFactory

    req = RequestFactory().post(
        "/", HTTP_X_FORWARDED_FOR="6.6.6.6", REMOTE_ADDR="10.0.0.1"
    )
    assert rate_limit.client_ip(req) == "10.0.0.1"
