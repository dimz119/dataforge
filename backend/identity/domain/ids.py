"""Application-side identifier authority for the Identity context (C-3).

UUIDv4 for accounts, UUIDv7 (RFC 9562, time-ordered) for tokens — both minted
in the app so ids exist before commit (database-schema §3.1/§3.2, C-3). Kept
local to the context so Identity never imports another app's infra.
"""

import secrets
import time
import uuid


def uuid4() -> uuid.UUID:
    """Random UUIDv4 — the default pk form (C-3)."""
    return uuid.uuid4()


def uuid7() -> uuid.UUID:
    """UUIDv7: 48-bit unix-ms timestamp + version/variant + 74 random bits.

    Time-ordered ids for the rows database-schema C-3 marks UUIDv7 (tokens).
    """
    ts_ms = time.time_ns() // 1_000_000
    value = (
        ((ts_ms & 0xFFFF_FFFF_FFFF) << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return uuid.UUID(int=value)
