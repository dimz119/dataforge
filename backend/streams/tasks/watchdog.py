"""The lease-expiry watchdog beat (backend-architecture §7.1 lifecycle; T4/T11).

A beat-scheduled task on the ``lifecycle`` queue (control plane only — it never
generates, ADR-0006). It enforces the failover-window guard from domain-model §4.3:

* **T4** — a stream in ``starting`` with no live lease acquired within **60 s** of
  the start command transitions to ``failed`` (``status_reason = error``). This is
  the "no runner acquires a lease within 60 s" guard.
* **T11** — a stream that was ``running`` but has lost its lease and not had it
  re-claimed within the failover window also fails. Full failover-exhaustion
  tracking (3 takeovers / 10 min) is a Phase-6+ refinement; Phase 5 implements the
  no-lease-past-window failed transition that the kill-test/exit-criteria need.

A successful crash failover is NOT a transition (domain-model §4.3 "crash without
state change"): another runner claims the expired lease (≤ 15 s detection) and
resumes; the stream stays ``running``. The watchdog only fires when NO live lease
appears past the window — distinguishing "failover in progress" (a live lease
exists, recently acquired) from "failover failed" (no live lease, window elapsed).

The lease presence read is advisory (Redis, ``streams.infra.leases``); on a
degraded cache it reports the lease present, so the watchdog never fails a stream on
a transient cache outage (fail-safe).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from celery import shared_task
from django.utils import timezone

from streams.application import services
from streams.domain.models import (
    LC_RESUMING,
    LC_RUNNING,
    LC_STARTING,
    MVP_SHARD_ID,
    REASON_ERROR,
    Stream,
)
from streams.infra import leases

logger = structlog.get_logger(__name__)

# The failover window (domain-model §4.3 T4): no lease within 60 s → failed. The
# lease TTL is 15 s (§8.2); a healthy failover re-claims within ≤ 17 s, well inside
# this window, so a stream still without a live lease at 60 s has genuinely failed.
FAILOVER_WINDOW = timedelta(seconds=60)

# Lifecycle states the watchdog supervises: converging toward / holding running.
_SUPERVISED = (LC_STARTING, LC_RUNNING, LC_RESUMING)

__all__ = ["FAILOVER_WINDOW", "lease_expiry_watchdog", "overdue_streams"]


def overdue_streams(*, now: Any = None) -> list[Stream]:
    """Streams past the failover window with no live lease (T4/T11 candidates).

    The batch the watchdog acts on: supervised lifecycle states whose last
    transition is older than the failover window AND whose shard has no live Redis
    lease. Unscoped (the watchdog is a platform supervisor spanning all workspaces;
    each row carries its workspace_id for the failed-transition write under
    workspace_scope).
    """
    from tenancy.application.services import platform_read_scope

    now = now or timezone.now()
    cutoff = now - FAILOVER_WINDOW
    # tenancy: unscoped — control-plane watchdog supervises every workspace's
    # streams; the failed transition is written per-row under its workspace context.
    # The cross-tenant candidate SELECT runs under platform_read_scope so the strict
    # Class T policy admits every workspace's rows to the NOBYPASSRLS runtime role
    # (read-only; the per-row failed-transition write below re-arms the real ws).
    with platform_read_scope():
        candidates = list(
            Stream.all_objects.filter(
                lifecycle_state__in=_SUPERVISED,
                last_transition_at__isnull=False,
                last_transition_at__lte=cutoff,
            )
        )
    overdue: list[Stream] = []
    for stream in candidates:
        if not leases.has_live_lease(stream.id, MVP_SHARD_ID):
            overdue.append(stream)
    return overdue


@shared_task(name="streams.lease_expiry_watchdog", queue="lifecycle")
def lease_expiry_watchdog() -> dict[str, int]:
    """Beat task: fail streams with no live lease past the failover window (T4/T11).

    Idempotent against current lifecycle state (INV-STR-3 spirit): a stream already
    ``failed`` is skipped by :func:`overdue_streams`'s filter and ``mark_failed``'s
    no-op. Each failed transition runs under its own workspace context (Layer-1
    contextvar + Layer-2 RLS GUC) so the Class-T RLS + audit GUCs hold under the
    NOBYPASSRLS runtime role (backend-architecture §4.2).
    """
    from tenancy.application.services import worker_workspace_scope

    failed = 0
    for stream in overdue_streams():
        with worker_workspace_scope(stream.workspace_id):
            # Re-read under the scoped context so the write is RLS-clean.
            scoped = Stream.objects.filter(id=stream.id).first()
            if scoped is None:
                continue
            services.mark_failed(stream=scoped, reason=REASON_ERROR, actor="system")
            failed += 1
    logger.info("lease_expiry_watchdog_done", failed=failed)
    return {"failed": failed}
