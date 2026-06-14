"""Application-side identifier authority for the Schema Registry context (C-3).

Subjects and schema versions use UUIDv4 primary keys (no time-ordering is needed
for these low-volume control-plane rows; database-schema §4.4-4.5). Ids are
minted app-side so they exist before commit — the publish transaction creates a
subject row and its version-1 row in one atomic block and references the new ids
across the registry write (R-DER, schema-registry §5.1).

Kept local to the context so Registry never imports another app's infra (the
import-linter cross-app contract).
"""

from __future__ import annotations

import uuid


def uuid4() -> uuid.UUID:
    """Random UUIDv4 — the pk form for ``schema_subjects`` / ``schema_versions``."""
    return uuid.uuid4()
