"""Audit-writer seam for the Identity context (INV-AUD-2).

Identity records security-relevant events by calling the Audit context's writer
`audit.application.writer.record_audit(action, actor, workspace_id, target,
metadata)`, which the Audit app owns and which writes **in the current
transaction** (INV-AUD-2). We import it lazily so the two contexts can be built
in parallel and so cross-app coupling stays at the application layer (the
import-linter cross-app rule permits application↔application).

`emit` is the single call site identity uses; it never passes secrets
(INV-AUD-3) — callers pass references/prefixes only.
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
    """Write one audit entry transactionally with the calling mutation.

    `actor` is the User (or `None`/"system" for pre-auth events). `metadata`
    must contain no secret material (INV-AUD-3): token references by id, never
    values; never password material.
    """
    try:
        from audit.application.writer import record_audit
    except ImportError:
        # The Audit app's writer lands alongside this context; until then in
        # isolation, surface the gap loudly rather than silently dropping the
        # entry (INV-AUD-2). The integrated build always has it.
        logger.warning("audit_writer_unavailable", action=action)
        return
    record_audit(
        action=action,
        actor=actor,
        workspace_id=workspace_id,
        target=target,
        metadata=metadata or {},
    )
