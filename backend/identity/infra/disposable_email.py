"""Disposable-email denylist (SEC-ACC-9).

Signup against a domain on the vendored disposable-domain denylist is rejected.
The full dataset is the `disposable-email-domains` list updated via Dependabot
(security §5.4); a representative starter set is vendored here so the gate is
live in Phase 2. The vendored file path can be overridden so the dataset refresh
is a data change, not a code change.

Controlled by `SIGNUP_DISPOSABLE_EMAIL_BLOCK` (default true in production).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from django.conf import settings

# Representative starter set (the Dependabot-refreshed dataset replaces/extends
# this via DISPOSABLE_EMAIL_DOMAINS_FILE). Lowercase, bare domains.
_VENDORED_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "trashmail.com",
        "yopmail.com",
        "throwawaymail.com",
        "getnada.com",
        "dispostable.com",
        "maildrop.cc",
        "fakeinbox.com",
        "sharklasers.com",
    }
)


@lru_cache(maxsize=1)
def _domains() -> frozenset[str]:
    """Load the denylist once: vendored set plus optional override file."""
    domains = set(_VENDORED_DOMAINS)
    override = getattr(settings, "DISPOSABLE_EMAIL_DOMAINS_FILE", "")
    if override:
        path = Path(override)
        if path.is_file():
            domains.update(
                line.strip().lower()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            )
    return frozenset(domains)


def is_disposable_email(email: str) -> bool:
    """True iff the address's domain is on the disposable denylist (SEC-ACC-9).

    Inert when `SIGNUP_DISPOSABLE_EMAIL_BLOCK` is false (the check is skipped by
    callers, but this stays a pure domain test).
    """
    _, _, domain = email.rpartition("@")
    return domain.lower() in _domains()
