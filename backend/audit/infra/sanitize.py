"""Defensive secret-stripping for audit metadata/target (INV-AUD-3).

No secret material ever lands in an audit entry: no key plaintext, no password
or password hash, no token value (INV-AUD-3, SEC-AUD-3). Callers are written to
pass references/prefixes only, but this is the *backstop* that holds even if a
caller is wrong — secrets must never be persisted to the immutable, admin-readable
log (which would be unredactable after the fact, INV-AUD-1).

The strategy is key-name based, recursive over nested dicts/lists: any key whose
(lowercased) name matches a secret-shaped token is dropped entirely and replaced
by a redaction marker so the *shape* of the offending payload is still visible
for forensics without leaking the value. This is conservative by design — it
prefers dropping a benign key over keeping a secret one.
"""

from __future__ import annotations

from typing import Any

# Substrings that mark a key as secret-shaped. Matched case-insensitively as
# substrings so e.g. ``api_key``, ``key_hash``, ``refresh_token``, ``password``,
# ``secret`` all redact. ``prefix``/``last4`` are explicitly *allowed* (they are
# the non-secret references the audit contract stores) and are never matched here.
_SECRET_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "plaintext",
    "private",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "key_hash",
    "keyhash",
    "hash",
    "salt",
    "signature",
    "cookie",
    "session",
)

_REDACTED = "[redacted]"


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def scrub(value: Any) -> Any:
    """Return a copy of ``value`` with secret-shaped keys redacted (INV-AUD-3).

    Recurses through dicts and lists/tuples. Non-container values pass through
    unchanged (the redaction is key-name driven, not value-content driven —
    value-content scanning would be both lossy and false-positive-prone).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            key = str(raw_key)
            if _is_secret_key(key):
                out[key] = _REDACTED
            else:
                out[key] = scrub(raw_val)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value
