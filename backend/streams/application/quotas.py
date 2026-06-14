"""Stream quota caps enforced at command time (INV-TEN-5; api-spec §4.8.1).

The two synchronously-checkable caps that gate a ``start`` (T2/T12 guard,
domain-model §4.3) and bound a ``target_tps`` at create:

* ``max_concurrent_streams`` — the number of the workspace's streams in a
  non-stopped/non-terminal lifecycle state may not exceed the plan cap.
* ``per_stream_tps_cap`` — a single stream's ``target_tps`` may not exceed the
  plan's per-stream ceiling.

Limits come from the workspace's ``workspace_quotas`` row (seeded Free-tier at
workspace creation; database-schema §3.7, PRD §7). Usage *metering* (events/day,
aggregate TPS) completes in Phase 11; the two caps here are exactly the
"synchronously checkable limits" the api-spec §4.4 names as enforceable now.

The "concurrent" count uses the lifecycle state, not desired state: a stream that
is ``starting``/``running``/``pausing``/``paused``/``resuming``/``stopping`` is
occupying a slot; ``created``/``stopped``/``failed`` are not (they are the three
deletable / idle states).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from streams.domain.models import (
    LC_CREATED,
    LC_FAILED,
    LC_STOPPED,
    Stream,
)

# Free-tier fallback caps (database-schema §3.7) when no quotas row exists.
_FREE_MAX_CONCURRENT = 2
_FREE_PER_STREAM_TPS = 50

# Lifecycle states that do NOT occupy a concurrent-stream slot.
_IDLE_STATES: frozenset[str] = frozenset({LC_CREATED, LC_STOPPED, LC_FAILED})

__all__ = ["StreamQuotaCaps", "StreamQuotaExceeded", "caps_for", "check_start_allowed"]


@dataclass(frozen=True)
class StreamQuotaCaps:
    """The two synchronously-checkable stream caps for a plan (PRD §7)."""

    max_concurrent_streams: int
    per_stream_tps_cap: int


class StreamQuotaExceeded(Exception):
    """A start/create would breach a plan cap. Carries the breached quota (403)."""

    def __init__(self, *, quota: str, limit: int, requested: int) -> None:
        super().__init__(
            f"{quota} {requested} exceeds the plan limit of {limit} (INV-TEN-5)"
        )
        self.quota = quota
        self.limit = limit
        self.requested = requested


def caps_for(workspace_id: UUID | Any) -> StreamQuotaCaps:
    """The stream caps for a workspace from its ``workspace_quotas`` row.

    Falls back to the Free-tier defaults when no row exists (defensive; every
    workspace is seeded one at creation). Read under the active workspace context.
    """
    from tenancy.domain.models import WorkspaceQuotas

    row = WorkspaceQuotas.all_objects.filter(  # tenancy: unscoped — own-workspace cap read by id
        workspace_id=workspace_id
    ).first()
    if row is None:
        return StreamQuotaCaps(
            max_concurrent_streams=_FREE_MAX_CONCURRENT,
            per_stream_tps_cap=_FREE_PER_STREAM_TPS,
        )
    return StreamQuotaCaps(
        max_concurrent_streams=row.max_concurrent_streams,
        per_stream_tps_cap=row.per_stream_tps_cap,
    )


def per_stream_tps_cap(workspace_id: UUID | Any) -> int:
    """The plan's per-stream TPS ceiling (used at create + PATCH, PIN-3)."""
    return caps_for(workspace_id).per_stream_tps_cap


def check_start_allowed(stream: Stream) -> None:
    """Raise :class:`StreamQuotaExceeded` if starting ``stream`` breaches a cap.

    Concurrent-stream cap: count the workspace's streams in an occupied lifecycle
    state, *excluding* this one (a restart from stopped does not double-count).
    Per-stream TPS cap: the stream's ``target_tps`` must be within the plan ceiling.
    """
    caps = caps_for(stream.workspace_id)
    if stream.target_tps > caps.per_stream_tps_cap:
        raise StreamQuotaExceeded(
            quota="per_stream_tps",
            limit=caps.per_stream_tps_cap,
            requested=stream.target_tps,
        )
    occupied = (
        Stream.objects.exclude(lifecycle_state__in=_IDLE_STATES)
        .exclude(id=stream.id)
        .count()
    )
    if occupied + 1 > caps.max_concurrent_streams:
        raise StreamQuotaExceeded(
            quota="concurrent_streams",
            limit=caps.max_concurrent_streams,
            requested=occupied + 1,
        )
