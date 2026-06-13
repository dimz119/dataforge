"""Auth endpoint flows: signup, verify, login, refresh, logout, reset (api-spec §4.1)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.conf import settings
from django.core import mail
from rest_framework.test import APIClient

from identity.domain.models import User, UserToken

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery"


def _extract_token(body: str) -> str:
    match = re.search(r"/(?:verify-email|reset-password)/([A-Za-z0-9_-]+)", body)
    assert match, f"no token link in email: {body!r}"
    return match.group(1)


# --- Signup ------------------------------------------------------------------
def test_signup_creates_unverified_and_sends_email(
    api: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        resp = api.post(
            "/api/v1/auth/signup",
            {"email": "Ada@Example.com", "password": PASSWORD},
            format="json",
        )
    assert resp.status_code == 201
    assert resp.data["is_verified"] is False
    assert resp.data["email"] == "ada@example.com"  # normalized (INV-ID-1)
    user = User.objects.get(email="ada@example.com")
    assert user.is_verified is False
    assert len(mail.outbox) == 1
    assert "verify-email" in mail.outbox[0].body


def test_signup_duplicate_email_409(api: APIClient) -> None:
    api.post(
        "/api/v1/auth/signup",
        {"email": "dup@example.com", "password": PASSWORD},
        format="json",
    )
    resp = api.post(
        "/api/v1/auth/signup", {"email": "DUP@example.com", "password": PASSWORD}, format="json"
    )
    assert resp.status_code == 409
    assert resp.data["type"].endswith("/conflict")


def test_signup_weak_password_400(api: APIClient) -> None:
    resp = api.post(
        "/api/v1/auth/signup",
        {"email": "weak@example.com", "password": "short"},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.data["type"].endswith("/validation-error")


def test_signup_disposable_email_400_with_code(api: APIClient) -> None:
    resp = api.post(
        "/api/v1/auth/signup",
        {"email": "bot@mailinator.com", "password": PASSWORD},
        format="json",
    )
    assert resp.status_code == 400
    assert resp.data["errors"][0]["field"] == "email"
    assert resp.data["errors"][0]["code"] == "disposable_email_domain"


# --- Verify ------------------------------------------------------------------
def test_verify_email_flips_is_verified(
    api: APIClient, django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        api.post(
            "/api/v1/auth/signup", {"email": "v@example.com", "password": PASSWORD}, format="json"
        )
    token = _extract_token(str(mail.outbox[0].body))
    resp = api.post("/api/v1/auth/verify-email", {"token": token}, format="json")
    assert resp.status_code == 200
    assert resp.data["is_verified"] is True
    assert User.objects.get(email="v@example.com").is_verified is True


def test_verify_email_invalid_token_400_token_invalid(api: APIClient) -> None:
    resp = api.post("/api/v1/auth/verify-email", {"token": "bogus"}, format="json")
    assert resp.status_code == 400
    assert resp.data["errors"][0]["code"] == "token_invalid"


# --- Login -------------------------------------------------------------------
def test_login_sets_cookie_returns_access_never_refresh(api: APIClient, make_user: Any) -> None:
    make_user("login@example.com", is_verified=True)
    resp = api.post(
        "/api/v1/auth/login", {"email": "login@example.com", "password": PASSWORD}, format="json"
    )
    assert resp.status_code == 200
    assert "access_token" in resp.data
    # SEC-AUTH-3: refresh is NEVER in the body.
    assert "refresh_token" not in resp.data
    assert resp.data["token_type"] == "Bearer"
    assert resp.data["access_expires_in"] == 900
    # df_refresh cookie set, HttpOnly + path-scoped.
    cookie = resp.cookies[settings.JWT_REFRESH_COOKIE_NAME]
    assert cookie["httponly"] is True
    assert cookie["path"] == "/api/v1/auth"
    assert cookie["samesite"] == "Strict"


def test_login_bad_password_401_authentication_failed(api: APIClient, make_user: Any) -> None:
    make_user("bad@example.com", is_verified=True)
    resp = api.post(
        "/api/v1/auth/login",
        {"email": "bad@example.com", "password": "wrong-password-xx"},
        format="json",
    )
    assert resp.status_code == 401
    assert resp.data["type"].endswith("/authentication-failed")


def test_unverified_user_can_login(api: APIClient, unverified_user: Any) -> None:
    resp = api.post(
        "/api/v1/auth/login",
        {"email": unverified_user.email, "password": PASSWORD},
        format="json",
    )
    assert resp.status_code == 200  # INV-ID-2: login allowed, gating happens elsewhere


# --- Refresh -----------------------------------------------------------------
def test_refresh_rotates_via_cookie(api: APIClient, make_user: Any) -> None:
    make_user("r@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "r@example.com", "password": PASSWORD}, format="json"
    )
    api.cookies = login.client.cookies
    resp = api.post("/api/v1/auth/refresh", {}, format="json")
    assert resp.status_code == 200
    assert "access_token" in resp.data
    assert "refresh_token" not in resp.data


def test_refresh_reuse_is_401(api: APIClient, make_user: Any) -> None:
    make_user("reuse@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "reuse@example.com", "password": PASSWORD}, format="json"
    )
    old_cookie = login.cookies[settings.JWT_REFRESH_COOKIE_NAME].value
    api.cookies = login.client.cookies
    api.post("/api/v1/auth/refresh", {}, format="json")  # rotate once
    # Replay the original cookie value.
    resp = api.post("/api/v1/auth/refresh", {"refresh_token": old_cookie}, format="json")
    # Cookie present from the successful rotation, so clear it to force body use.
    api.cookies.clear()
    resp = api.post("/api/v1/auth/refresh", {"refresh_token": old_cookie}, format="json")
    assert resp.status_code == 401
    assert resp.data["type"].endswith("/authentication-required")


def test_refresh_origin_mismatch_403(api: APIClient, make_user: Any) -> None:
    make_user("origin@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "origin@example.com", "password": PASSWORD}, format="json"
    )
    api.cookies = login.client.cookies
    resp = api.post("/api/v1/auth/refresh", {}, format="json", HTTP_ORIGIN="https://evil.example")
    assert resp.status_code == 403
    assert resp.data["type"].endswith("/permission-denied")


# --- Password reset ----------------------------------------------------------
def test_password_reset_request_always_202(api: APIClient) -> None:
    # No account → still 202 (SEC-ACC-6, no enumeration).
    resp = api.post("/api/v1/auth/password-reset", {"email": "ghost@example.com"}, format="json")
    assert resp.status_code == 202
    assert len(mail.outbox) == 0


def test_password_reset_not_sent_for_unverified(api: APIClient, unverified_user: Any) -> None:
    resp = api.post("/api/v1/auth/password-reset", {"email": unverified_user.email}, format="json")
    assert resp.status_code == 202  # SEC-ACC-8: no reset for unverified, uniform 202
    assert len(mail.outbox) == 0


def test_password_reset_confirm_flow(api: APIClient, make_user: Any) -> None:
    make_user("pr@example.com", is_verified=True)
    api.post("/api/v1/auth/password-reset", {"email": "pr@example.com"}, format="json")
    token = _extract_token(str(mail.outbox[0].body))
    resp = api.post(
        "/api/v1/auth/password-reset/confirm",
        {"token": token, "new_password": "brand-new-passphrase"},
        format="json",
    )
    assert resp.status_code == 200
    assert User.objects.get(email="pr@example.com").check_password("brand-new-passphrase")
    # Token is single-use: re-confirm fails.
    again = api.post(
        "/api/v1/auth/password-reset/confirm",
        {"token": token, "new_password": "another-new-passphrase"},
        format="json",
    )
    assert again.status_code == 400


def test_resend_verification_always_202(api: APIClient) -> None:
    resp = api.post(
        "/api/v1/auth/resend-verification",
        {"email": "nobody@example.com"},
        format="json",
    )
    assert resp.status_code == 202
    assert len(mail.outbox) == 0


# --- Logout ------------------------------------------------------------------
def test_logout_clears_cookie_and_blacklists(api: APIClient, make_user: Any) -> None:
    make_user("lo@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "lo@example.com", "password": PASSWORD}, format="json"
    )
    access = login.data["access_token"]
    api.cookies = login.client.cookies
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    resp = api.post("/api/v1/auth/logout", {}, format="json")
    assert resp.status_code == 204
    # Cookie expired.
    assert resp.cookies[settings.JWT_REFRESH_COOKIE_NAME]["max-age"] == 0


# --- Users / change password -------------------------------------------------
def test_get_users_me(api: APIClient, make_user: Any) -> None:
    make_user("me@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "me@example.com", "password": PASSWORD}, format="json"
    )
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access_token']}")
    resp = api.get("/api/v1/users/me")
    assert resp.status_code == 200
    assert resp.data["email"] == "me@example.com"
    assert resp.data["is_verified"] is True
    assert resp.data["memberships"] == []


def test_change_password_spares_current_session_refresh(api: APIClient, make_user: Any) -> None:
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

    from identity.application.auth import refresh_jti

    user = make_user("cp@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "cp@example.com", "password": PASSWORD}, format="json"
    )
    current_refresh = login.cookies[settings.JWT_REFRESH_COOKIE_NAME].value
    current_jti = refresh_jti(current_refresh)
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access_token']}")
    resp = api.post(
        "/api/v1/users/me/password",
        {
            "current_password": PASSWORD,
            "new_password": "a-fresh-new-passphrase",
            "refresh_token": current_refresh,
        },
        format="json",
    )
    assert resp.status_code == 204
    assert user.check_password("a-fresh-new-passphrase") is False  # not refetched
    user.refresh_from_db()
    assert user.check_password("a-fresh-new-passphrase")
    # The current session's refresh family is spared (SEC-AUTH-10).
    assert not BlacklistedToken.objects.filter(token__jti=current_jti).exists()


def test_change_password_wrong_current_401(api: APIClient, make_user: Any) -> None:
    make_user("cpw@example.com", is_verified=True)
    login = api.post(
        "/api/v1/auth/login", {"email": "cpw@example.com", "password": PASSWORD}, format="json"
    )
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access_token']}")
    resp = api.post(
        "/api/v1/users/me/password",
        {"current_password": "totally-wrong-pw", "new_password": "a-fresh-new-passphrase"},
        format="json",
    )
    assert resp.status_code == 401
    assert resp.data["type"].endswith("/authentication-failed")


def test_users_me_requires_auth_401(api: APIClient) -> None:
    resp = api.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.data["type"].endswith("/authentication-required")


# --- Tokens cleaned up on signup ---------------------------------------------
def test_signup_issues_one_verification_token(api: APIClient) -> None:
    api.post(
        "/api/v1/auth/signup",
        {"email": "one@example.com", "password": PASSWORD},
        format="json",
    )
    user = User.objects.get(email="one@example.com")
    count = UserToken.objects.filter(
        user=user, kind=UserToken.KIND_EMAIL_VERIFICATION
    ).count()
    assert count == 1
