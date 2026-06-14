"""Application-side identifier authority for the Scenario Catalog context (C-3).

All catalog rows (scenarios, manifest versions, scenario instances) use UUIDv4
primary keys — these are low-volume control-plane rows with no time-ordering
requirement (database-schema §4.1-4.3). Ids are minted app-side so they exist
before commit: the publish transaction references the new ManifestVersion id when
stamping ``derived_from_definition`` on the schema versions it registers in the
same atomic block (R-DER-4, schema-registry §5.1).

Kept local to the context so Catalog never imports another app's infra (the
import-linter cross-app contract).
"""

from __future__ import annotations

import uuid


def uuid4() -> uuid.UUID:
    """Random UUIDv4 — the pk form for every catalog row (C-3)."""
    return uuid.uuid4()
