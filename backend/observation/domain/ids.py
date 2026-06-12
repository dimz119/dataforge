"""Identifier helpers for the Observation context.

UUIDv7 (RFC 9562) request ids: time-ordered, mintable at ingress, the
support-ticket join key (observability §3.1).
"""

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7: 48-bit unix-ms timestamp + 74 random bits."""
    ts_ms = time.time_ns() // 1_000_000
    value = (
        ((ts_ms & 0xFFFF_FFFF_FFFF) << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return uuid.UUID(int=value)
