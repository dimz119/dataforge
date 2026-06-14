"""Audit-writer seam for the Stream Control context (INV-AUD-2).

Stream Control records security-relevant lifecycle mutations (stream create, the
start/stop desired-state writes, the watchdog ``failed`` transition) by calling
the Audit context's writer in the **current transaction** (INV-AUD-2). Imported
lazily so cross-app coupling stays at the application layer (the import-linter
cross-app rule permits application↔application).

Callers pass ids / slugs / states only — never secret material (INV-AUD-3).
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
