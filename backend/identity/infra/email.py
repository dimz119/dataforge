"""Account-lifecycle email integration (security §5.1/§5.2).

Sends verification and password-reset emails through Django's configured email
backend (EMAIL_URL → Mailpit SMTP in dev; deployment §2.3). The plaintext token
appears only inside the email body (security §5); it is never stored, logged, or
returned in any API response. Links point at the console (`CONSOLE_BASE_URL`)
with the token as a path parameter over HTTPS (SEC-TLS-6 redaction in logs).
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string


def _console_url(path: str) -> str:
    base = settings.CONSOLE_BASE_URL.rstrip("/")
    return f"{base}{path}"


def send_verification_email(*, to_email: str, token: str) -> None:
    """Email a single-use 24 h verification link (security §5.1)."""
    link = _console_url(f"/verify-email/{token}")
    context = {"link": link, "ttl_hours": 24, "email": to_email}
    body = render_to_string("identity/email/verify_email.txt", context)
    send_mail(
        subject="Verify your DataForge email",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )


def send_password_reset_email(*, to_email: str, token: str) -> None:
    """Email a single-use 1 h password-reset link (security §5.2)."""
    link = _console_url(f"/reset-password/{token}")
    context = {"link": link, "ttl_hours": 1, "email": to_email}
    body = render_to_string("identity/email/password_reset.txt", context)
    send_mail(
        subject="Reset your DataForge password",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )


def send_password_changed_notice(*, to_email: str) -> None:
    """Email a 'your password was changed' notice (security §5.2 tripwire)."""
    body = render_to_string("identity/email/password_changed.txt", {"email": to_email})
    send_mail(
        subject="Your DataForge password was changed",
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )
