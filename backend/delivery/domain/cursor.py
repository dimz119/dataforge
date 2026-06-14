"""The REST cursor codec — the ``c1.`` opaque replay cursor (delivery-channels §5.2;
database-schema §6.1).

A cursor encodes the composite position ``(partition_ts, buffer_seq)`` of the last
delivered row (exclusive on read) plus a fingerprint ``f`` binding the cursor to its
stream **and** the filter set it was created under (RC-7). The encoding is normative:

    plain  = canonical JSON, sorted keys, no whitespace:
             {"f":"<fingerprint>","p":<partition_ts_epoch_ms>,"s":<buffer_seq>}
    cursor = "c1." + base64url_without_padding(utf8(plain))

    p = the row's partition_ts as epoch milliseconds (BW-6)
    s = the row's per-stream buffer_seq (BW-6)
    f = first 8 lowercase hex chars of SHA-256(stream_id || "|" || canonical_filter_set)

This module is *framework-light* domain code: stdlib only (``base64``, ``hashlib``,
``json``) — no Django, no DRF. The opacity contract (RC-7) is enforced here: callers
treat cursors as opaque tokens ≤ 128 chars; the server is the only party that
encodes/decodes, and the encoding changes only under a version-prefix bump
(``c1.`` → ``c2.``), with old prefixes decodable for ≥ 90 days.

The fingerprint binds the cursor to ``(stream_id, filter set)``: presenting a ``c1.``
cursor against a different stream or a different filter set fails decode with a
:class:`CursorDecodeError` of kind ``"fingerprint"`` (the API layer maps it to
``400 cursor-invalid``, RC-8). Undecodable / unknown-prefix tokens fail with kind
``"format"`` (also ``400 cursor-invalid``). Expiry (§5.4) is a position check the API
layer performs *after* a successful decode, never here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass

__all__ = [
    "CURSOR_MAX_LEN",
    "CURSOR_PREFIX",
    "CursorDecodeError",
    "CursorPosition",
    "decode_cursor",
    "encode_cursor",
    "filter_fingerprint",
]

# The normative version prefix (RC-7). A future encoding bumps to ``c2.``; old
# prefixes remain decodable for ≥ 90 days.
CURSOR_PREFIX = "c1."

# Opacity bound (RC-7 / domain-model Cursor VO): a cursor is a URL-safe token
# ≤ 128 chars. A presented token longer than this is rejected at decode (format).
CURSOR_MAX_LEN = 128

# Fingerprint width: first 8 lowercase hex chars of the SHA-256 (§5.2).
_FINGERPRINT_HEX_LEN = 8


@dataclass(frozen=True)
class CursorPosition:
    """A decoded cursor position (delivery-channels §5.2).

    ``p`` is the row's ``partition_ts`` as epoch milliseconds and ``s`` the
    per-stream ``buffer_seq`` — together the composite ``(partition_ts, buffer_seq)``
    the page query advances over (exclusive). ``f`` is the verified fingerprint.
    """

    p: int  # partition_ts epoch ms
    s: int  # buffer_seq
    f: str  # the stream+filter fingerprint (already verified by decode)


class CursorDecodeError(Exception):
    """A cursor failed to decode (delivery-channels RC-8).

    ``kind`` distinguishes a structural failure (``"format"`` — undecodable token,
    unknown prefix, bad JSON, missing/typed-wrong members, over-long) from a
    binding failure (``"fingerprint"`` — a well-formed cursor presented against a
    different stream or filter set). Both map to ``400 cursor-invalid`` at the API
    layer; the distinction is for diagnostics/tests only.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def filter_fingerprint(*, stream_id: str, canonical_filter_set: str) -> str:
    """The ``f`` member: first 8 lowercase hex of SHA-256(stream || "|" || filters).

    Binds a cursor to its stream AND the canonical filter set it was created under
    (RC-7). ``canonical_filter_set`` must be the *canonical* string form (the API
    layer owns canonicalization — e.g. a sorted, comma-joined ``types`` list — so
    equivalent filter sets map to one fingerprint and the page query is gap-free
    over the filter set, RC-4).
    """
    material = f"{stream_id}|{canonical_filter_set}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return digest[:_FINGERPRINT_HEX_LEN]


def encode_cursor(*, p: int, s: int, fingerprint: str) -> str:
    """Encode ``(p, s, fingerprint)`` to the opaque ``c1.`` token (§5.2).

    ``plain`` is canonical JSON (sorted keys, no whitespace) so re-encoding the same
    position yields a byte-identical cursor — the property the replay contract
    (INV-DEL-3) and the conformance round-trip test depend on.
    """
    plain = json.dumps(
        {"f": fingerprint, "p": p, "s": s},
        sort_keys=True,
        separators=(",", ":"),
    )
    body = base64.urlsafe_b64encode(plain.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"{CURSOR_PREFIX}{body}"


def decode_cursor(token: str, *, expected_fingerprint: str) -> CursorPosition:
    """Decode + verify a ``c1.`` token against ``expected_fingerprint`` (RC-8).

    Raises :class:`CursorDecodeError`:

    * ``kind="format"`` — over-long, missing/unknown prefix, non-base64url body,
      non-JSON / non-object payload, or a missing/mistyped ``f``/``p``/``s``.
    * ``kind="fingerprint"`` — a structurally valid cursor whose ``f`` does not
      equal ``expected_fingerprint`` (wrong stream or wrong filter set).

    The position bounds are *not* range-checked beyond type/sign here — a far-future
    or past ``p`` is a valid position; expiry (§5.4) is the API layer's job.
    """
    if not isinstance(token, str) or len(token) > CURSOR_MAX_LEN:
        raise CursorDecodeError("Cursor exceeds the maximum token length.", kind="format")
    if not token.startswith(CURSOR_PREFIX):
        raise CursorDecodeError("Unknown or missing cursor version prefix.", kind="format")
    body = token[len(CURSOR_PREFIX) :]
    if not body:
        raise CursorDecodeError("Empty cursor body.", kind="format")

    # base64url decode (re-pad: we strip padding on encode).
    padding = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + padding)
    except (binascii.Error, ValueError) as exc:
        raise CursorDecodeError("Cursor body is not valid base64url.", kind="format") from exc

    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorDecodeError("Cursor body is not valid JSON.", kind="format") from exc

    if not isinstance(payload, dict):
        raise CursorDecodeError("Cursor payload is not an object.", kind="format")

    f = payload.get("f")
    p = payload.get("p")
    s = payload.get("s")
    # bool is an int subclass — reject it explicitly so {"p": true} is a format error.
    if not isinstance(f, str) or isinstance(p, bool) or isinstance(s, bool):
        raise CursorDecodeError("Cursor members have the wrong type.", kind="format")
    if not isinstance(p, int) or not isinstance(s, int) or p < 0 or s < 0:
        raise CursorDecodeError("Cursor position members are invalid.", kind="format")

    if f != expected_fingerprint:
        raise CursorDecodeError(
            "Cursor was issued for a different stream or filter set.", kind="fingerprint"
        )
    return CursorPosition(p=p, s=s, f=f)
