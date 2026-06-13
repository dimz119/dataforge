"""API-key cryptography + format (security §3.2.1; database-schema §3.6).

Key format ``df_<env>_<prefix>_<secret>`` (frozen, security §3.2.1):

* ``env``    — environment token ``live | stg | dev`` from the server's ``DF_ENV``
  (``dev`` ⇒ ``dev``, ``staging`` ⇒ ``stg``, ``prod`` ⇒ ``live``); SEC-KEY-2
  compares the presented token against this mapping.
* ``prefix`` — 8 chars ``[a-z0-9]``, unique per key, stored plaintext (the O(1)
  lookup handle). The stored ``key_prefix`` column is the full public part
  ``df_<env>_<prefix>`` (database-schema §3.6).
* ``secret`` — 30 chars base62 from a CSPRNG (≈ 178 bits entropy).

Storage is ``sha256(full key string)`` + ``key_prefix`` + ``last4`` only
(SEC-KEY-3): SHA-256 (not Argon2) is correct for a high-entropy machine secret —
fast verification at data-plane rates, preimage resistance is all that is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from django.conf import settings

_PREFIX_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_SECRET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_PREFIX_LEN = 8
_SECRET_LEN = 30

# DF_ENV → wire env token (security §3.2.1). prod ⇒ live, staging ⇒ stg, dev ⇒ dev.
_ENV_TOKENS: dict[str, str] = {"prod": "live", "staging": "stg", "dev": "dev"}


def env_token() -> str:
    """The wire env token for this server (SEC-KEY-2 compares against it)."""
    return _ENV_TOKENS.get(settings.DF_ENV, "dev")


@dataclass(frozen=True)
class GeneratedKey:
    """A freshly minted key: the plaintext (shown once) plus its stored parts."""

    plaintext: str  # df_<env>_<prefix>_<secret> — the only place the secret exists
    key_prefix: str  # 'df_<env>_<prefix>' — the durable lookup handle (unique)
    short_prefix: str  # the 8-char prefix alone (api-spec list `prefix` field)
    key_hash: str  # sha256(plaintext) hex
    last4: str  # last 4 of the secret


def _random(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_key(plaintext: str) -> str:
    """SHA-256 hex of the full key string (SEC-KEY-3)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def keys_match(plaintext: str, key_hash: str) -> bool:
    """Constant-time compare of ``sha256(plaintext)`` against the stored hash."""
    return hmac.compare_digest(hash_key(plaintext), key_hash)


def generate_key() -> GeneratedKey:
    """Mint a new ``df_<env>_<prefix>_<secret>`` key (security §3.2.1)."""
    env = env_token()
    short = _random(_PREFIX_ALPHABET, _PREFIX_LEN)
    secret = _random(_SECRET_ALPHABET, _SECRET_LEN)
    plaintext = f"df_{env}_{short}_{secret}"
    return GeneratedKey(
        plaintext=plaintext,
        key_prefix=f"df_{env}_{short}",
        short_prefix=short,
        key_hash=hash_key(plaintext),
        last4=secret[-4:],
    )


@dataclass(frozen=True)
class ParsedKey:
    """The parsed components of a presented key (no validation of existence)."""

    env: str
    short_prefix: str
    key_prefix: str  # 'df_<env>_<prefix>' — matches the stored column
    secret: str


def parse_key(presented: str) -> ParsedKey | None:
    """Parse ``df_<env>_<prefix>_<secret>``; ``None`` on any structural fault.

    No state oracle: callers map ``None`` to the single ``invalid-api-key`` 401.
    """
    parts = presented.split("_")
    if len(parts) != 4:
        return None
    df, env, short, secret = parts
    if df != "df" or not env or len(short) != _PREFIX_LEN or len(secret) != _SECRET_LEN:
        return None
    return ParsedKey(env=env, short_prefix=short, key_prefix=f"df_{env}_{short}", secret=secret)
