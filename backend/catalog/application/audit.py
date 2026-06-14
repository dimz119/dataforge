"""Audit-writer seam for the Scenario Catalog context (INV-AUD-2).

Catalog records security-relevant mutations (scenario publish, instance config
edits) by calling the Audit context's writer in the **current transaction**
(INV-AUD-2). Imported lazily so cross-app coupling stays at the application layer
(the import-linter cross-app rule permits application↔application).

Minimum audited action set this context emits (domain-model §2.10):
``catalog.scenario.published`` (one per published manifest version) and
``registry.schema_version.registered`` (one per derived+registered schema version,
named explicitly in the minimum set, schema-registry §5). Callers pass ids /
slugs / versions only — never secret material (INV-AUD-3).
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def emit(
    action: str,
    *,
    actor: Any,
    workspace_id: Any | None = None,
    target: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write one audit entry transactionally with the calling mutation (INV-AUD-2)."""
    try:
        from audit.application.writer import record_audit
    except ImportError:
        logger.warning("audit_writer_unavailable", action=action)
        return
    record_audit(
        action=action,
        actor=actor,
        workspace_id=workspace_id,
        target=target,
        metadata=metadata or {},
    )
