"""Lifecycle command-handler tasks (backend-architecture §7.1 lifecycle queue).

Control-plane supervision commands on the ``lifecycle`` queue (ADR-0006: commands
ABOUT streams, never the streams themselves). These are the system-initiated halves
of the lifecycle state machine (domain-model §4.3) — the user-initiated halves
(start/stop) run synchronously in the API request (the verb returns 200 immediately;
reconciliation is the runner's job).

* ``streams.system_pause_stream`` — T5 system pause: quota exhaustion
  (``status_reason = quota``) or idle auto-pause (``status_reason = idle``), driven
  by Observation signals. Phase 6 owns the FULL pause convergence; Phase 5 writes the
  desired-state + reason + audit (a thin pass-through, the runner halts).
* ``streams.fail_stream`` — the async failed transition (T4/T11) when a supervisor
  other than the beat watchdog decides a stream has failed (e.g. a non-retryable
  runner error reported via Observation). Idempotent against current state.

Every task is idempotent against the current lifecycle state (§7.1: "tasks
idempotent against current lifecycle state, INV-STR-3") and runs the mutation under
the stream's workspace scope (Layer-1 contextvar + Layer-2 RLS GUC) so the
NOBYPASSRLS runtime role can write it (backend-architecture §4.2).
"""

from __future__ import annotations

from uuid import UUID

import structlog
from celery import shared_task

from streams.application import services
from streams.domain.models import (
    REASON_ERROR,
    REASON_IDLE,
    REASON_QUOTA,
    Stream,
)

logger = structlog.get_logger(__name__)

__all__ = ["fail_stream", "system_pause_stream"]

_PAUSE_REASONS = frozenset({REASON_QUOTA, REASON_IDLE})


def _scoped_stream(stream_id: UUID) -> Stream | None:
    return Stream.objects.filter(id=stream_id).first()


@shared_task(name="streams.system_pause_stream", queue="lifecycle")
def system_pause_stream(stream_id: str, reason: str) -> dict[str, str]:
    """T5 system pause (quota/idle): write desired = paused + the system reason.

    Delegates to :func:`streams.application.services.system_pause`, which writes the
    desired ``paused`` + system ``status_reason`` (rendered ``paused_quota`` /
    ``paused_idle``), audits ``streams.stream.system_paused {reason}`` in the same
    transaction (INV-AUD-2), and bumps ``df_quota_pauses_total{reason}`` (P11-07).
    Only pauses a live stream (the T5 source states); a no-op otherwise (idempotent).
    ``reason`` must be ``quota`` or ``idle``; anything else defaults to ``quota``.
    """
    from tenancy.application.services import worker_workspace_scope

    sid = UUID(str(stream_id))
    pause_reason = reason if reason in _PAUSE_REASONS else REASON_QUOTA
    # tenancy: unscoped — control-plane supervisor resolves the workspace by unique id.
    owner = Stream.all_objects.filter(id=sid).values_list("workspace_id", flat=True).first()
    if owner is None:
        return {"stream_id": str(sid), "result": "not_found"}
    with worker_workspace_scope(owner):
        stream = _scoped_stream(sid)
        if stream is None:
            return {"stream_id": str(sid), "result": "not_found"}
        before = stream.desired_state
        stream = services.system_pause(stream=stream, reason=pause_reason)
        if stream.desired_state == before:
            return {"stream_id": str(sid), "result": "noop"}
    logger.info("system_pause_stream_done", stream_id=str(sid), reason=pause_reason)
    return {"stream_id": str(sid), "result": "paused", "reason": pause_reason}


@shared_task(name="streams.fail_stream", queue="lifecycle")
def fail_stream(stream_id: str, reason: str = REASON_ERROR) -> dict[str, str]:
    """T4/T11 async failed transition (idempotent against current state)."""
    from tenancy.application.services import worker_workspace_scope

    sid = UUID(str(stream_id))
    # tenancy: unscoped — control-plane supervisor resolves the workspace by unique id.
    owner = Stream.all_objects.filter(id=sid).values_list("workspace_id", flat=True).first()
    if owner is None:
        return {"stream_id": str(sid), "result": "not_found"}
    with worker_workspace_scope(owner):
        stream = _scoped_stream(sid)
        if stream is None:
            return {"stream_id": str(sid), "result": "not_found"}
        services.mark_failed(stream=stream, reason=reason, actor="system")
    logger.info("fail_stream_done", stream_id=str(sid), reason=reason)
    return {"stream_id": str(sid), "result": "failed", "reason": reason}
