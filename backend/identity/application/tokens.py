"""Token issuance and single-use consumption (INV-ID-3, security §5).

Issuing a token of a kind supersedes prior unconsumed tokens of that kind
(consumed_at set) in the same transaction; consumption burns the token once.
All work runs under `transaction.atomic` so supersession + insert are atomic.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from identity.domain.models import User, UserToken
from identity.infra.tokens import generate_token, hash_token

# TTLs are part of the contract (database-schema §3.2; security §5.1/§5.2).
_TTL: dict[str, timedelta] = {
    UserToken.KIND_EMAIL_VERIFICATION: timedelta(hours=24),
    UserToken.KIND_PASSWORD_RESET: timedelta(hours=1),
}


@transaction.atomic
def issue_token(user: User, kind: str) -> str:
    """Supersede prior unconsumed tokens of `kind`, mint a new one, return plaintext.

    Returns the plaintext (for the email only); the row stores only the hash.
    """
    now = timezone.now()
    # Supersession (INV-ID-3): any still-live token of this kind is consumed now.
    UserToken.objects.filter(user=user, kind=kind, consumed_at__isnull=True).update(
        consumed_at=now
    )
    plaintext, token_hash = generate_token()
    UserToken.objects.create(
        user=user,
        kind=kind,
        token_hash=token_hash,
        expires_at=now + _TTL[kind],
    )
    return plaintext


@transaction.atomic
def consume_token(plaintext: str, kind: str) -> UserToken | None:
    """Burn a live token of `kind` and return it; `None` if invalid/expired/used.

    Looks up by hash (the unique column), verifies it is live (unconsumed and
    not past TTL, INV-ID-3), then sets `consumed_at` — single-use. The compare
    is a hash lookup; timing is constant across hit/miss because the work is the
    same digest + indexed query either way.
    """
    token_hash = hash_token(plaintext)
    now = timezone.now()
    token = (
        UserToken.objects.select_for_update()
        .filter(token_hash=token_hash, kind=kind)
        .first()
    )
    if token is None or not token.is_live(now=now):
        return None
    token.consumed_at = now
    token.save(update_fields=["consumed_at"])
    return token


def invalidate_tokens(user: User, kind: str) -> int:
    """Mark all of a user's unconsumed tokens of `kind` consumed (e.g. on reset)."""
    return UserToken.objects.filter(
        user=user, kind=kind, consumed_at__isnull=True
    ).update(consumed_at=timezone.now())
