"""StreamStats read service — assembles ``GET /api/v1/streams/{id}/stats`` (§4.11.1).

The application-layer orchestrator over the two infra reads (Redis counters +
``event_buffer`` window) and the per-stream control/clock facts the caller supplies.
It owns the §4.11.1 response shape and the ``health`` derivation; it does **not**
touch the broker, Postgres rows, or the lease (the caller resolves the stream + the
runner-liveness signal under its own RLS/auth context, then hands the facts in).

Cross-app cleanliness: this lives in ``delivery.application`` so the ``streams`` API
view (and any other caller) goes through the application boundary — it never imports
``delivery.infra`` directly. Inputs are primitives so the service carries no
``streams`` model dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from delivery.infra import buffer_stats, stream_stats

__all__ = ["StreamControlFacts", "build_stream_stats"]

# A stream is "live" (health derivable) only in these lifecycle states (§4.11.1:
# "health is null for streams not running/pausing/resuming").
_LIVE_LIFECYCLE = frozenset({"running", "pausing", "resuming"})

# health = "stale" when the counters are older than this (§4.11.1).
_STALE_AFTER_S = 30


@dataclass(frozen=True)
class StreamControlFacts:
    """The control/clock facts the caller resolves from the stream row + lease.

    Keeps the service free of a ``streams`` model import: the API view (which already
    resolved + RLS-scoped the row) passes the surfaced ``status``, the desired
    ``target_tps``, the virtual-clock fields, and whether a live runner lease exists
    (``runner_alive`` — the §4.11.1 heartbeat-fresh signal).
    """

    stream_id: str
    status: str
    lifecycle_state: str
    target_tps: int
    virtual_now: str | None
    speed_multiplier: float
    runner_alive: bool


def _parse_rfc3339(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _derive_health(
    *,
    facts: StreamControlFacts,
    snapshot: stream_stats.StreamStatsSnapshot,
    now: datetime,
) -> str | None:
    """The closed ``health`` enum (§4.11.1).

    ``null`` for a non-live stream; otherwise ``healthy`` when the runner lease is
    fresh (≤ 15 s, the lease TTL) and the counters are current; ``stale`` when the
    last event is older than 30 s (Observation itself lagging); ``degraded`` when the
    runner heartbeat gap is showing (no live lease / counters absent — failover in
    progress, domain-model §4.3 "crash without state change").
    """
    if facts.lifecycle_state not in _LIVE_LIFECYCLE:
        return None
    if not facts.runner_alive or not snapshot.present:
        return "degraded"
    if snapshot.last_event_at is not None:
        last = _parse_rfc3339(snapshot.last_event_at)
        if last is not None and (now - last).total_seconds() > _STALE_AFTER_S:
            return "stale"
    return "healthy"


def build_stream_stats(
    *, workspace_id: str, facts: StreamControlFacts
) -> dict[str, object]:
    """Assemble the §4.11.1 stats response for one stream.

    Reads the Redis counters (≤ 5 s stale, INV-OBS-2) and the ``event_buffer``
    window (the caller has armed the workspace, INV-OBS-3), then renders the response
    with the derived ``health``. Counters absent (never delivered, or pre-rebuild
    after a Redis loss) read as zero with ``health="degraded"`` for a live stream.
    """
    snapshot = stream_stats.read_stats(
        workspace_id=workspace_id, stream_id=facts.stream_id
    )
    window = buffer_stats.read_buffer_window(stream_id=facts.stream_id)
    now = datetime.now(UTC)
    as_of = f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond:06d}Z"

    return {
        "stream_id": facts.stream_id,
        "status": facts.status,
        "health": _derive_health(facts=facts, snapshot=snapshot, now=now),
        "total_events": snapshot.total_events,
        "observed_tps": snapshot.observed_tps,
        "target_tps": facts.target_tps,
        "last_event_at": snapshot.last_event_at,
        "by_event_type": dict(sorted(snapshot.by_event_type.items())),
        "buffer": {
            "earliest_available_at": window.earliest_available_at,
            "latest_event_at": window.latest_event_at,
            "retention_hours": window.retention_hours,
        },
        "virtual_clock": {
            "virtual_now": facts.virtual_now,
            "speed_multiplier": facts.speed_multiplier,
        },
        "as_of": as_of,
    }
