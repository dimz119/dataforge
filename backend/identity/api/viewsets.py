"""Identity API views (api-spec §4.1 auth, §4.2 users).

JWT-only console surface (ADR-0011). Public auth endpoints use AllowAny; the
rest require an authenticated JWT. The refresh token is transported via the
df_refresh cookie (SEC-AUTH-3); the access token is returned in the body.
All responses route their errors through the shared RFC 9457 handler with the
closed slugs (config.problems).
"""

from __future__ import annotations

from typing import Any, cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from config.problems import AuthenticationRequired, PermissionDeniedError, RateLimited
from identity.api import cookies, serializers
from identity.application import accounts, auth
from identity.domain.models import User
from identity.infra import captcha as captcha_infra
from identity.infra import disposable_email, rate_limit
from identity.infra.jwt import DataForgeJWTAuthentication


def _validated(serializer_cls: type, request: Request) -> dict[str, Any]:
    serializer = serializer_cls(data=request.data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _user(request: Request) -> User:
    """The authenticated identity User (IsAuthenticated guarantees the runtime type)."""
    return cast(User, request.user)


class SignupView(APIView):
    """POST /auth/signup — create unverified account, send verification (§4.1)."""

    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.SignupRequestSerializer,
        responses={201: serializers.SignupResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        # Abuse seam (security §5.4), enforced before any account work:
        # 1) per-IP rate limit (RL-1), 2) disposable-email denylist (SEC-ACC-9),
        # 3) captcha hook (SEC-ACC-10, default `none`).
        ip = rate_limit.client_ip(request._request)
        result = rate_limit.check("signup", ip, rate_limit.signup_windows())
        if not result.allowed:
            raise RateLimited(retry_after=result.retry_after)

        data = _validated(serializers.SignupRequestSerializer, request)
        email = data["email"]

        if settings.SIGNUP_DISPOSABLE_EMAIL_BLOCK and disposable_email.is_disposable_email(email):
            # SEC-ACC-9: errors[0] = {field: email, code: disposable_email_domain}.
            detail = ErrorDetail(
                "Disposable email domains are not allowed.", code="disposable_email_domain"
            )
            raise ValidationError({"email": [detail]})
        if captcha_infra.captcha_required():
            try:
                captcha_infra.verify_captcha(data.get("captcha_token"), remote_ip=ip)
            except captcha_infra.CaptchaError as exc:
                raise ValidationError({"captcha_token": [str(exc)]}) from exc

        user = accounts.register_user(email=email, password=data["password"])
        body = serializers.SignupResponseSerializer(
            {
                "user_id": user.id,
                "email": user.email,
                "is_verified": user.is_verified,
                "created_at": user.created_at,
            }
        ).data
        return Response(body, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.VerifyEmailRequestSerializer,
        responses={200: serializers.VerifyEmailResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        data = _validated(serializers.VerifyEmailRequestSerializer, request)
        user = accounts.verify_email(token=data["token"])
        return Response({"user_id": str(user.id), "is_verified": user.is_verified})


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.EmailOnlyRequestSerializer,
        responses={202: serializers.DetailResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        data = _validated(serializers.EmailOnlyRequestSerializer, request)
        accounts.resend_verification(email=data["email"])  # uniform 202 (SEC-ACC-6)
        return Response(
            {"detail": "If the address exists and is unverified, a new email was sent."},
            status=status.HTTP_202_ACCEPTED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.LoginRequestSerializer,
        responses={200: serializers.TokenPairResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        data = _validated(serializers.LoginRequestSerializer, request)
        refresh, _user = auth.login(
            email=data["email"], password=data["password"], request=request._request
        )
        response = Response(cookies.token_pair_body(refresh))
        return cookies.set_refresh_cookie(response, refresh)


class RefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    def _refresh_token(self, request: Request) -> str:
        # Cookie is canonical; the body field is the documented fallback (§4.1).
        cookie = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)
        if cookie:
            return cookie
        serializer = serializers.RefreshRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data.get("refresh_token")
        if not token:
            raise AuthenticationRequired("No refresh token provided.")
        return str(token)

    def _check_origin(self, request: Request) -> None:
        # SEC-AUTH-4: validate Origin against the console allowlist (CSRF defense).
        origin = request.headers.get("Origin")
        if origin is None:
            return  # non-browser clients (curl/tests) carry no Origin
        if origin not in settings.CONSOLE_ORIGINS:
            raise PermissionDeniedError("Origin not allowed.")

    @extend_schema(
        request=serializers.RefreshRequestSerializer,
        responses={200: serializers.TokenPairResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        self._check_origin(request)
        raw = self._refresh_token(request)
        refresh, _user = auth.rotate_refresh(raw)
        response = Response(cookies.token_pair_body(refresh))
        return cookies.set_refresh_cookie(response, refresh)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=serializers.LogoutRequestSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        raw = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME) or request.data.get(
            "refresh_token", ""
        )
        if raw:
            auth.logout(str(raw), actor=_user(request))
        response = Response(status=status.HTTP_204_NO_CONTENT)
        return cookies.clear_refresh_cookie(response)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.EmailOnlyRequestSerializer,
        responses={202: serializers.DetailResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        data = _validated(serializers.EmailOnlyRequestSerializer, request)
        accounts.request_password_reset(email=data["email"])  # uniform 202 (SEC-ACC-6)
        return Response(
            {"detail": "If the address belongs to a verified account, a reset email was sent."},
            status=status.HTTP_202_ACCEPTED,
        )


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[type] = []

    @extend_schema(
        request=serializers.PasswordResetConfirmRequestSerializer,
        responses={200: serializers.DetailResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        data = _validated(serializers.PasswordResetConfirmRequestSerializer, request)
        accounts.confirm_password_reset(token=data["token"], new_password=data["new_password"])
        return Response({"detail": "Password updated."})


class UserMeView(APIView):
    """GET/PATCH /users/me and POST /users/me/password / DELETE /users/me (§4.2).

    Memberships are injected by the Tenancy app, which subclasses or wraps this
    view to populate the `memberships` array; Identity returns the account core.
    """

    # JWT-only console surface (SEC-AUTH-1): the global default lists
    # ApiKeyAuthentication first, so this view pins JWT-only auth — an API key here
    # is the wrong credential type, treated as absent → 401, never parsed as a
    # machine principal (which would otherwise hit the account serializer).
    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: serializers.UserMeResponseSerializer})
    def get(self, request: Request) -> Response:
        memberships = getattr(request, "membership_summaries", None)
        return Response(accounts.serialize_self(_user(request), memberships=memberships))

    @extend_schema(request=serializers.DeleteAccountRequestSerializer, responses={204: None})
    def delete(self, request: Request) -> Response:
        data = _validated(serializers.DeleteAccountRequestSerializer, request)
        # Sole-admin guard (INV-ID-4 / INV-TEN-3) is owned by Tenancy; it is
        # invoked here through an optional hook so this endpoint stays functional
        # before the tenancy guard lands, and enforces it once present.
        user = _user(request)
        guard = getattr(request, "sole_admin_guard", None)
        if callable(guard):
            guard(user)  # raises ConflictError naming blocking workspaces
        accounts.request_account_deletion(user=user, password=data["password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChangePasswordView(APIView):
    # JWT-only console surface (SEC-AUTH-1): an API key here → absent → 401.
    authentication_classes: list[type] = [DataForgeJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(request=serializers.ChangePasswordRequestSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        data = _validated(serializers.ChangePasswordRequestSerializer, request)
        # The current session's *refresh* jti (not the access jti) identifies the
        # family to spare (SEC-AUTH-10). Resolved from the supplied refresh token;
        # the df_refresh cookie does not reach this path.
        raw_refresh = data.get("refresh_token") or request.COOKIES.get(
            settings.JWT_REFRESH_COOKIE_NAME, ""
        )
        current_jti = auth.refresh_jti(raw_refresh) if raw_refresh else None
        accounts.change_password(
            user=_user(request),
            current_password=data["current_password"],
            new_password=data["new_password"],
            current_jti=current_jti,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
