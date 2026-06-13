"""URL routing for the Identity API (api-spec §4.1 auth, §4.2 users).

Mounted under /api/v1 by config.urls.
"""

from django.urls import path

from identity.api import viewsets

auth_urlpatterns = [
    path("auth/signup", viewsets.SignupView.as_view(), name="auth-signup"),
    path("auth/verify-email", viewsets.VerifyEmailView.as_view(), name="auth-verify-email"),
    path(
        "auth/resend-verification",
        viewsets.ResendVerificationView.as_view(),
        name="auth-resend-verification",
    ),
    path("auth/login", viewsets.LoginView.as_view(), name="auth-login"),
    path("auth/refresh", viewsets.RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout", viewsets.LogoutView.as_view(), name="auth-logout"),
    path(
        "auth/password-reset",
        viewsets.PasswordResetRequestView.as_view(),
        name="auth-password-reset",
    ),
    path(
        "auth/password-reset/confirm",
        viewsets.PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
]

users_urlpatterns = [
    path("users/me", viewsets.UserMeView.as_view(), name="users-me"),
    path("users/me/password", viewsets.ChangePasswordView.as_view(), name="users-me-password"),
]

urlpatterns = [*auth_urlpatterns, *users_urlpatterns]
