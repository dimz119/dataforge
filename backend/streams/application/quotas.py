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
_FREE_AGGREGATE_TPS = 100
_FREE_EVENTS_PER_DAY = 1_000_000
# Concurrent backfills per workspace (scaling §5; PRD §7). Plan-flat for MVP.
MAX_CONCURRENT_BACKFILLS = 2

# Lifecycle states that do NOT occupy a concurrent-stream slot.
_IDLE_STATES: frozenset[str] = frozenset({LC_CREATED, LC_STOPPED, LC_FAILED})

__all__ = [
    "MAX_CONCURRENT_BACKFILLS",
    "StreamQuotaCaps",
    "StreamQuotaExceeded",
    "aggregate_tps_cap",
    "caps_for",
    "check_aggregate_tps_allowed",
    "check_concurrent_backfills_allowed",
    "check_start_allowed",
    "events_per_day_cap",
    "per_stream_tps_cap",
]


@dataclass(frozen=True)
class StreamQuotaCaps:
    """The four plan caps gating a stream command (PRD §7; scaling §5)."""

    max_concurrent_streams: int
    per_stream_tps_cap: int
    aggregate_tps_cap: int
    events_per_day: int


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
            aggregate_tps_cap=_FREE_AGGREGATE_TPS,
            events_per_day=_FREE_EVENTS_PER_DAY,
        )
    return StreamQuotaCaps(
        max_concurrent_streams=row.max_concurrent_streams,
        per_stream_tps_cap=row.per_stream_tps_cap,
        aggregate_tps_cap=row.aggregate_tps_cap,
        events_per_day=row.events_per_day,
    )


def per_stream_tps_cap(workspace_id: UUID | Any) -> int:
    """The plan's per-stream TPS ceiling (used at create + PATCH, PIN-3)."""
    return caps_for(workspace_id).per_stream_tps_cap


def aggregate_tps_cap(workspace_id: UUID | Any) -> int:
    """The plan's workspace-aggregate TPS ceiling (Σ running target_tps; PRD §7)."""
    return caps_for(workspace_id).aggregate_tps_cap


def events_per_day_cap(workspace_id: UUID | Any) -> int:
    """The plan's events/day ceiling (events delivered per UTC day; PRD §7)."""
    return caps_for(workspace_id).events_per_day


def check_start_allowed(stream: Stream) -> None:
    """Raise :class:`StreamQuotaExceeded` if starting ``stream`` breaches a cap.

    Three command-time caps gate a start (INV-TEN-5):

    * Per-stream TPS cap: the stream's ``target_tps`` must be within the plan ceiling.
    * Concurrent-stream cap: count the workspace's streams in an occupied lifecycle
      state, *excluding* this one (a restart from stopped does not double-count).
    * Aggregate-TPS cap: Σ ``target_tps`` over the workspace's already-occupied
      streams plus this one must stay within the plan's aggregate ceiling.

    All three read the workspace's own rows under the active scoped context — no
    cross-tenant read (the workspace is already armed by the command's transaction).
    """
    caps = caps_for(stream.workspace_id)
    if stream.target_tps > caps.per_stream_tps_cap:
        raise StreamQuotaExceeded(
            quota="per_stream_tps",
            limit=caps.per_stream_tps_cap,
            requested=stream.target_tps,
        )
    occupied_qs = Stream.objects.exclude(lifecycle_state__in=_IDLE_STATES).exclude(
        id=stream.id
    )
    occupied = occupied_qs.count()
    if occupied + 1 > caps.max_concurrent_streams:
        raise StreamQuotaExceeded(
            quota="concurrent_streams",
            limit=caps.max_concurrent_streams,
            requested=occupied + 1,
        )
    check_aggregate_tps_allowed(stream, _occupied_qs=occupied_qs, _caps=caps)


def check_aggregate_tps_allowed(
    stream: Stream,
    *,
    target_tps: int | None = None,
    _occupied_qs: Any | None = None,
    _caps: StreamQuotaCaps | None = None,
) -> None:
    """Raise :class:`StreamQuotaExceeded` if Σ running target_tps exceeds the plan cap.

    The workspace-aggregate ceiling (PRD §7): the sum of ``target_tps`` over the
    workspace's occupied streams (excluding ``stream``) plus ``stream``'s provisioned
    value must stay within ``aggregate_tps_cap``. ``target_tps`` overrides the
    stream's stored value for the prospective check at PATCH time (the new desired
    value, not the current one). Reads the workspace's own rows under the active
    scoped context.
    """
    from django.db.models import Sum

    caps = _caps or caps_for(stream.workspace_id)
    prospective = stream.target_tps if target_tps is None else target_tps
    qs = (
        _occupied_qs
        if _occupied_qs is not None
        else Stream.objects.exclude(lifecycle_state__in=_IDLE_STATES).exclude(id=stream.id)
    )
    other_total = int(qs.aggregate(total=Sum("target_tps"))["total"] or 0)
    if other_total + prospective > caps.aggregate_tps_cap:
        raise StreamQuotaExceeded(
            quota="aggregate_tps",
            limit=caps.aggregate_tps_cap,
            requested=other_total + prospective,
        )


def check_concurrent_backfills_allowed(workspace_id: UUID | Any, *, in_flight: int) -> None:
    """Raise :class:`StreamQuotaExceeded` if a new backfill exceeds the concurrent cap.

    ``in_flight`` is the count of the workspace's backfill jobs in a non-terminal
    (queued/running) state; admitting one more must stay within
    :data:`MAX_CONCURRENT_BACKFILLS` (scaling §5; PRD §7).
    """
    if in_flight + 1 > MAX_CONCURRENT_BACKFILLS:
        raise StreamQuotaExceeded(
            quota="concurrent_backfills",
            limit=MAX_CONCURRENT_BACKFILLS,
            requested=in_flight + 1,
        )
