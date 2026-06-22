"""Idle auto-pause beat (P11-07; PRD §7; domain-model §4.3 T5 system trigger).

A beat-scheduled task on the ``lifecycle`` queue (control plane only — it never
generates, ADR-0006). It conserves platform capacity by system-pausing streams
that are running but have not delivered an event for longer than the workspace's
``idle_pause_minutes`` threshold (Free default 120 min; database-schema §3.7):

* a running stream whose last delivery is older than its plan's idle threshold (or
  which has never delivered, and has been running past the threshold) is paused to
  ``paused_idle`` (NEVER deleted — INV-TEN-5), audited
  ``streams.stream.system_paused {reason: idle}`` with ``actor="system"``, and
  bumps ``df_quota_pauses_total{reason="idle"}`` (via ``services.system_pause``);
* the pause is one-click reversible from the console (a normal ``resume``; the idle
  reason carries no headroom guard, unlike a quota pause — there is always headroom
  to resume an idle stream).

Idleness uses two signals, both fail-safe: the stream's ``last_transition_at`` (the
floor — a just-started stream is never idle until the threshold elapses) and the
per-stream Redis ``last_event_at`` (the last delivery wall time; absent → the stream
has not delivered, so the transition floor governs). A degraded stats cache reports
no last-event time, so the transition floor alone decides — the task never pauses a
stream it cannot prove idle within the window (fail-safe, mirroring the watchdog).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from celery import shared_task
from django.utils import timezone

from streams.application import services
from streams.domain.models import LC_RUNNING, Stream

logger = structlog.get_logger(__name__)

__all__ = ["idle_auto_pause", "idle_streams"]

# The default idle threshold when a workspace has no quotas row (Free; §3.7).
_DEFAULT_IDLE_MINUTES = 120


def _parse_rfc3339_ms(raw: str) -> datetime | None:
    """Parse the RFC-3339 ``last_event_at`` wall string the stats hash stores."""
    try:
        text = raw.replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.get_current_timezone())
    return moment


def _idle_threshold(workspace_id: Any) -> timedelta:
    """The workspace's idle threshold from its quotas row (Free default; §3.7)."""
    from tenancy.domain.models import WorkspaceQuotas

    row = WorkspaceQuotas.all_objects.filter(  # tenancy: own-workspace cap read by id
        workspace_id=workspace_id
    ).first()
    minutes = row.idle_pause_minutes if row is not None else _DEFAULT_IDLE_MINUTES
    return timedelta(minutes=max(1, int(minutes)))


def _is_idle(stream: Stream, *, now: datetime) -> bool:
    """Has ``stream`` been running with no delivery past its idle threshold?

    The transition floor (``last_transition_at``) gates first: a stream that became
    running within the threshold is never idle yet. Past that, the per-stream
    ``last_event_at`` decides — a delivery within the window keeps it active; no
    recent delivery (or none at all) makes it idle. A missing transition timestamp
    is treated as not-idle (fail-safe).
    """
    from delivery.infra import stream_stats

    threshold = _idle_threshold(stream.workspace_id)
    cutoff = now - threshold
    if stream.last_transition_at is None or stream.last_transition_at > cutoff:
        return False  # just started / no clock → not yet idle (fail-safe floor)
    snapshot = stream_stats.read_stats(
        workspace_id=str(stream.workspace_id), stream_id=str(stream.id)
    )
    if snapshot.last_event_at is None:
        # No delivery recorded: running past the threshold with no events → idle.
        return True
    last = _parse_rfc3339_ms(snapshot.last_event_at)
    if last is None:
        return True  # unparseable timestamp → fall back to the transition floor
    return last <= cutoff


def idle_streams(*, now: datetime | None = None) -> list[Stream]:
    """The running streams past their idle threshold with no recent delivery.

    Unscoped platform read (the auto-pause supervisor spans every workspace); the
    pause write below re-arms each row's workspace. The candidate SELECT runs under
    ``platform_read_scope`` so the strict Class-T RLS policy admits every workspace's
    running streams to the NOBYPASSRLS runtime role (read-only).
    """
    from tenancy.application.services import platform_read_scope

    now = now or timezone.now()
    with platform_read_scope():
        candidates = list(
            Stream.all_objects.filter(  # tenancy: platform-wide idle scan (read-only)
                lifecycle_state=LC_RUNNING,
                desired_state="running",
            )
        )
    return [s for s in candidates if _is_idle(s, now=now)]


@shared_task(name="streams.idle_auto_pause", queue="lifecycle")
def idle_auto_pause() -> dict[str, int]:
    """Beat task: pause running streams idle past their threshold → ``paused_idle``.

    Each pause runs under its own workspace context (Layer-1 contextvar + Layer-2
    RLS GUC) so the Class-T RLS + audit GUCs hold under the NOBYPASSRLS runtime role.
    Idempotent: ``services.system_pause`` is a no-op for a stream already paused-
    desired or no longer running, so a re-run pauses nothing new.
    """
    from tenancy.application.services import worker_workspace_scope

    paused = 0
    for stream in idle_streams():
        with worker_workspace_scope(stream.workspace_id):
            scoped = Stream.objects.filter(id=stream.id).first()
            if scoped is None:
                continue
            before = scoped.desired_state
            services.system_pause(stream=scoped, reason="idle")
            if scoped.desired_state != before:
                paused += 1
    logger.info("idle_auto_pause_done", paused=paused)
    return {"paused": paused}
