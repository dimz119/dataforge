"""df_refresh cookie transport + token-pair body shaping (SEC-AUTH-2/3).

The refresh token is set ONLY as the `df_refresh` cookie:
HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth; Max-Age=604800. It never
appears in a response body. The access token is returned in the body and held in
memory by the SPA.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from django.conf import settings
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

_SameSite = Literal["Lax", "Strict", "None"]


def token_pair_body(refresh: RefreshToken) -> dict[str, Any]:
    """The login/refresh JSON body: access token only, no refresh (SEC-AUTH-3)."""
    return {
        "access_token": str(refresh.access_token),
        "token_type": "Bearer",
        "access_expires_in": settings.JWT_ACCESS_TTL,
        "refresh_expires_in": settings.JWT_REFRESH_TTL,
    }


def set_refresh_cookie(response: Response, refresh: RefreshToken) -> Response:
    """Attach the df_refresh cookie per SEC-AUTH-3."""
    response.set_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        value=str(refresh),
        max_age=settings.JWT_REFRESH_TTL,
        httponly=True,
        secure=settings.JWT_REFRESH_COOKIE_SECURE,
        samesite=cast(_SameSite, settings.JWT_REFRESH_COOKIE_SAMESITE),
        path=settings.JWT_REFRESH_COOKIE_PATH,
    )
    return response


def clear_refresh_cookie(response: Response) -> Response:
    """Expire the df_refresh cookie (Max-Age=0) on logout (SEC-AUTH-5)."""
    response.delete_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        path=settings.JWT_REFRESH_COOKIE_PATH,
        samesite=cast(_SameSite, settings.JWT_REFRESH_COOKIE_SAMESITE),
    )
    return response
