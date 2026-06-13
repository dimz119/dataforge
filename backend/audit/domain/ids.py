"""Application-side identifier authority for the Audit context (C-3).

Audit entries use UUIDv7 (RFC 9562, time-ordered) so ``audit_id`` sorts by
creation time and aligns with the monthly ``occurred_at`` RANGE partitioning
(database-schema §3 / §7.1, §8.1). Minted app-side so the id exists before
commit (C-3). Kept local to the context so Audit never imports another app's
infra (the import-linter cross-app contract).
"""

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    """UUIDv7: 48-bit unix-ms timestamp + version/variant + 74 random bits.

    Time-ordered ids for the rows database-schema C-3 marks UUIDv7 (audit
    entries). Identical algorithm to the other contexts' ``uuid7`` — duplicated
    rather than imported to keep the bounded context self-contained.
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
