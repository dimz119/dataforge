"""Serializers for the Identity context — the payload boundary (api-spec §4.1/§4.2).

Request serializers validate shape only (the policy lives in services); response
serializers fix the documented JSON for drf-spectacular so the OpenAPI artifact
matches the api spec exactly.
"""

from __future__ import annotations

from rest_framework import serializers


# --- Auth requests -----------------------------------------------------------
class SignupRequestSerializer(serializers.Serializer[dict[str, str]]):
    email = serializers.EmailField()
    password = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False)
    # SEC-ACC-10: present only when a captcha provider is enabled; ignored under `none`.
    captcha_token = serializers.CharField(required=False, allow_blank=True)


class LoginRequestSerializer(serializers.Serializer[dict[str, str]]):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)


class VerifyEmailRequestSerializer(serializers.Serializer[dict[str, str]]):
    token = serializers.CharField()


class EmailOnlyRequestSerializer(serializers.Serializer[dict[str, str]]):
    """resend-verification / password-reset request body."""

    email = serializers.EmailField()
    captcha_token = serializers.CharField(required=False, allow_blank=True)


class RefreshRequestSerializer(serializers.Serializer[dict[str, str]]):
    # Optional: the refresh token is normally read from the df_refresh cookie.
    refresh_token = serializers.CharField(required=False, allow_blank=True)


class LogoutRequestSerializer(serializers.Serializer[dict[str, str]]):
    refresh_token = serializers.CharField(required=False, allow_blank=True)


class PasswordResetConfirmRequestSerializer(serializers.Serializer[dict[str, str]]):
    token = serializers.CharField()
    new_password = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False)


class ChangePasswordRequestSerializer(serializers.Serializer[dict[str, str]]):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(min_length=10, max_length=128, trim_whitespace=False)
    # Optional: the current session's refresh token, so its family is spared on
    # change (SEC-AUTH-10). The df_refresh cookie is path-scoped to /api/v1/auth
    # and is NOT sent to /users/me/password, so the SPA passes it explicitly to
    # stay logged in; omitting it revokes every refresh family (still correct,
    # just logs the caller out everywhere).
    refresh_token = serializers.CharField(required=False, allow_blank=True)


class DeleteAccountRequestSerializer(serializers.Serializer[dict[str, str]]):
    password = serializers.CharField(trim_whitespace=False)


# --- Auth responses ----------------------------------------------------------
class SignupResponseSerializer(serializers.Serializer[dict[str, object]]):
    user_id = serializers.UUIDField()
    email = serializers.EmailField()
    is_verified = serializers.BooleanField()
    created_at = serializers.DateTimeField()


class VerifyEmailResponseSerializer(serializers.Serializer[dict[str, object]]):
    user_id = serializers.UUIDField()
    is_verified = serializers.BooleanField()


class TokenPairResponseSerializer(serializers.Serializer[dict[str, object]]):
    """Login / refresh body. The refresh token is **never** in the body —
    it rides the `df_refresh` HttpOnly cookie (SEC-AUTH-3); only the access
    token is returned, held in memory by the SPA (SEC-AUTH-2)."""

    access_token = serializers.CharField()
    token_type = serializers.CharField(default="Bearer")
    access_expires_in = serializers.IntegerField()
    refresh_expires_in = serializers.IntegerField()


class DetailResponseSerializer(serializers.Serializer[dict[str, str]]):
    detail = serializers.CharField()


# --- Users -------------------------------------------------------------------
class MembershipSummarySerializer(serializers.Serializer[dict[str, str]]):
    workspace_id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()
    role = serializers.CharField()


class UserMeResponseSerializer(serializers.Serializer[dict[str, object]]):
    user_id = serializers.UUIDField()
    email = serializers.EmailField()
    is_verified = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    memberships = MembershipSummarySerializer(many=True)
