"""Comparison form + fingerprint for registry schema documents (schema-registry §6.1).

The *comparison form* of a stored schema strips every annotation key (SD-5:
``$schema``, ``$id``, ``title``, ``description``, ``$comment``, ``examples``, and
the platform ``x-df-binding``) at every nesting level, then canonicalizes
(RFC 8785 / JCS: sorted keys, no insignificant whitespace, UTF-8). "Identical",
fingerprints, and every §6.2 compatibility check operate on comparison forms — so
a ``description``- or binding-only change is *no change* (R-DER-4 no-op detection
and Flow-2 idempotency are database guarantees via the unique fingerprint, §3.2).

``fingerprint`` is the lowercase-hex SHA-256 of the comparison form's canonical
bytes. The JCS canonicalization here is a pure-Python implementation sufficient
for the closed-document profile (objects with string keys, arrays, strings,
integers, finite numbers, booleans, null — SD-3); it sorts object keys and emits
the compact separators RFC 8785 requires. Pure logic — no Django imports.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# SD-5 annotation keys, excluded from comparison form at every level.
ANNOTATION_KEYS: frozenset[str] = frozenset(
    {"$schema", "$id", "title", "description", "$comment", "examples", "x-df-binding"}
)


def comparison_form(document: Any) -> Any:
    """Recursively strip SD-5 annotation keys, returning the comparison form."""
    if isinstance(document, dict):
        return {
            key: comparison_form(value)
            for key, value in document.items()
            if key not in ANNOTATION_KEYS
        }
    if isinstance(document, list):
        return [comparison_form(item) for item in document]
    return document


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 (JCS) canonical bytes for ``value`` (sorted keys, compact)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(document: Any) -> str:
    """Lowercase-hex SHA-256 of the comparison form's canonical bytes (§3.2)."""
    return hashlib.sha256(canonical_bytes(comparison_form(document))).hexdigest()
