"""Application-side identifier authority for the Tenancy context (C-3).

UUIDv4 for workspaces, memberships, api_keys, quotas (no time ordering needed);
UUIDv7 (RFC 9562, time-ordered) for invitations (database-schema §3.5). All
minted app-side so ids exist before commit — required for the workspace-creation
RLS flow (database-schema §9.4): the new workspace id is set into the
``app.workspace_id`` GUC *before* the INSERT so the ``WITH CHECK`` policy passes.

Kept local to the context so Tenancy never imports another app's infra.
"""

import secrets
import time
import uuid


def uuid4() -> uuid.UUID:
    """Random UUIDv4 — the default pk form for non-time-ordered rows (C-3)."""
    return uuid.uuid4()


def uuid7() -> uuid.UUID:
    """UUIDv7: 48-bit unix-ms timestamp + version/variant + 74 random bits.

    Time-ordered ids for the rows database-schema C-3 marks UUIDv7 (invitations).
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
