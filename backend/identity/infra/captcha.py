"""Captcha verification hook (SEC-ACC-10).

A server-side verification interface with providers `none | turnstile`, selected
by `SIGNUP_CAPTCHA_PROVIDER` (default `none`). When enabled, signup and
password-reset-request require a valid captcha token. The interface and the
Turnstile implementation ship in Phase 2; enabling it is a config flip reserved
for observed abuse (the abuse runbook's documented first response).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

_TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class CaptchaError(Exception):
    """Captcha verification failed (missing or invalid token)."""


def captcha_required() -> bool:
    """True when a captcha provider other than `none` is configured (SEC-ACC-10)."""
    return getattr(settings, "SIGNUP_CAPTCHA_PROVIDER", "none") != "none"


def verify_captcha(token: str | None, *, remote_ip: str | None = None) -> None:
    """Raise `CaptchaError` unless the captcha is satisfied.

    Provider `none`: always passes (the MVP default). Provider `turnstile`:
    server-side siteverify against Cloudflare with the configured secret.
    """
    provider = getattr(settings, "SIGNUP_CAPTCHA_PROVIDER", "none")
    if provider == "none":
        return
    if provider == "turnstile":
        _verify_turnstile(token, remote_ip=remote_ip)
        return
    # Unknown provider is a misconfiguration; fail closed (never silently allow).
    raise CaptchaError(f"unsupported captcha provider: {provider}")


def _verify_turnstile(token: str | None, *, remote_ip: str | None) -> None:
    if not token:
        raise CaptchaError("captcha token is required")
    secret = getattr(settings, "TURNSTILE_SECRET_KEY", "")
    if not secret:
        raise CaptchaError("captcha provider is enabled but no secret is configured")
    payload: dict[str, str] = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(_TURNSTILE_VERIFY_URL, data=data)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Fail closed: a verification we cannot complete is not a pass.
        logger.warning("turnstile_verify_failed", error=str(exc))
        raise CaptchaError("captcha verification could not be completed") from exc
    if not body.get("success", False):
        raise CaptchaError("captcha verification failed")
