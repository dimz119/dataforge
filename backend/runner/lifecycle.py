"""Runner-converged lifecycle + stats seams (backend-architecture §8.3 steps "report
lifecycle" + 9; ADR-0006).

The control plane writes *desired* state; the runner converges and writes the
``lifecycle_state`` under its fencing token (domain-model §4.3). Two thin host
seams the shard worker uses:

* :func:`report_lifecycle` — write the runner-observed ``lifecycle_state`` (+
  ``status_reason``) for one stream: ``starting → running`` at T3, ``stopping →
  stopped`` at T10. The runner owns this column (services.py only *nudges* it);
  this is the convergence write. It carries no Redis fencing-token comparison
  (the Stream row is single-writer-per-shard by the lease; the durable fence lives
  in ``stream_shards.fencing_token`` + the checkpoint conditional write).
* :func:`incr_emitted` — bump the per-stream Redis events counter (INV-OBS-2,
  §8.3 step 9). Best-effort: a counter miss never fails a tick.

Blocking ORM work runs behind ``asyncio.to_thread`` so the asyncio tick loop
never blocks. This module is a Django host seam (the engine owns no ORM).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from streams.domain.models import (
    LC_RUNNING,
    LC_STOPPED,
    REASON_NONE,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger("dataforge.runner.lifecycle")

__all__ = ["incr_emitted", "report_lifecycle", "stats_key"]

# Redis per-stream emitted-events counter key (INV-OBS-2). Read by Observation.
_STATS_KEY = "df:stats:emitted:{stream_id}"


def stats_key(stream_id: str) -> str:
    """The Redis emitted-events counter key for a stream (§8.3 step 9)."""
    return _STATS_KEY.format(stream_id=stream_id)


async def report_lifecycle(
    stream_id: str,
    lifecycle_state: str,
    *,
    status_reason: str = REASON_NONE,
    workspace_id: str | None = None,
) -> None:
    """Converge the stream's ``lifecycle_state`` (the runner's column, §8.3).

    ``starting → running`` (T3) on the first tick; ``stopping → stopped`` (T10) at
    finalize. Idempotent: re-writing the same state is harmless. Runs in a thread
    (blocking ORM) so the tick loop is never blocked.

    ``workspace_id`` arms the row's tenant so the UPDATE matches under the Class T
    RLS policy when the runner runs as the NOBYPASSRLS runtime role (the row carries
    its own ``workspace_id``; the shard worker supplies it from the desired-state
    pin). It is optional only so existing tests on SQLite (RLS no-op) keep working.
    """
    await asyncio.to_thread(
        _report_lifecycle_sync, stream_id, lifecycle_state, status_reason, workspace_id
    )


def _report_lifecycle_sync(
    stream_id: str,
    lifecycle_state: str,
    status_reason: str,
    workspace_id: str | None = None,
) -> None:
    import uuid as _uuid

    from django.utils import timezone

    from streams.domain.models import Stream
    from tenancy.application.services import worker_workspace_scope

    now = timezone.now()
    ws = _uuid.UUID(str(workspace_id)) if workspace_id else None

    def _do_update() -> int:
        # Single-writer by the lease; the convergence write targets the runner-owned
        # lifecycle column for this stream's row (its workspace is armed below).
        return Stream.all_objects.filter(id=stream_id).update(
            lifecycle_state=lifecycle_state,
            status_reason=status_reason,
            last_transition_at=now,
            updated_at=now,
        )

    # The runner data-plane convergence write runs under the row's armed workspace so
    # the Class T USING clause matches it for the NOBYPASSRLS runtime role (§4.2).
    if ws is not None:
        with worker_workspace_scope(ws):
            updated = _do_update()
    else:
        updated = _do_update()
    if not updated:
        logger.warning("lifecycle.report_no_row", stream_id=stream_id, state=lifecycle_state)


async def incr_emitted(redis: Redis, stream_id: str, count: int) -> None:
    """Bump the per-stream emitted-events counter by ``count`` (best-effort)."""
    if count <= 0:
        return
    try:
        await redis.incrby(stats_key(stream_id), count)
    except Exception as exc:
        logger.warning("stats.incr_degraded", stream_id=stream_id, error=str(exc))


# Re-export the running/stopped target states the worker reports, so callers import
# the convergence vocabulary from one place.
RUNNING = LC_RUNNING
STOPPED = LC_STOPPED
