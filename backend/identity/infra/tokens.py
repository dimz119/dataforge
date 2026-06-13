"""Verification / reset token cryptography (security §5).

Tokens are 32-byte URL-safe CSPRNG values; only their SHA-256 hex digest is
stored (`user_tokens.token_hash`), and comparison is by hashing the presented
plaintext and looking it up (the hash column is unique). The plaintext exists
only inside the email. Constant-time comparison guards the digest compare.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_TOKEN_NBYTES = 32  # 256 bits of CSPRNG entropy (security §5)


def generate_token() -> tuple[str, str]:
    """Return `(plaintext, token_hash)`; only the hash is ever persisted."""
    plaintext = secrets.token_urlsafe(_TOKEN_NBYTES)
    return plaintext, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of the token plaintext (the stored lookup handle)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def tokens_match(plaintext: str, token_hash: str) -> bool:
    """Constant-time compare of `sha256(plaintext)` against a stored digest."""
    return hmac.compare_digest(hash_token(plaintext), token_hash)
