"""Redis-resident per-stream StreamStats counters (observability §5; database-schema
§5.6; INV-OBS-2/INV-OBS-3).

The tenant-facing product surface of observability — ``total_events``,
``by_event_type``, ``observed_tps`` (sliding window), ``last_event_at`` — held in
Redis so the read path (``GET /api/v1/streams/{id}/stats``) is ≤ 5 s stale
(INV-OBS-2) without touching Postgres.

**Canonical counting point (binding decision).** Counters are incremented on the
**buffer-writer sink path** — :meth:`BufferWriterChannel.deliver` calls
:func:`record_delivered_batch` *after* the durable ``event_buffer`` commit. The
buffer-writer is the single counting point (not the ws-pusher) because it is the
REST replay's source of truth: counting exactly the rows that landed in
``event_buffer`` makes the stats tally reconcile byte-for-byte with what
``GET /events`` will ever serve (observability §5 "stats reconcile with an
independent consumer-side tally", the Phase-6 XCH exit criterion). The ws-pusher
is the bulk-drop tail (drop-oldest) and would over- or under-count; it stays out
of the counting path. Counting after commit (not before) means a write that rolled
back never inflates the counters.

**Keyspace (observability §5; database-schema §5.6 ``stats:{stream_id}``).** Every
key embeds the owning ``workspace_id`` *and* ``stream_id`` (INV-TEN-1 / INV-OBS-3):

* ``df:ws:{workspace_id}:stream:{stream_id}:stats`` — a hash holding ``total_events``,
  ``last_event_at`` (RFC-3339 µs), and one ``type:{event_type}`` field per emitted
  type (the ``by_event_type`` map);
* ``df:ws:{workspace_id}:stream:{stream_id}:tps`` — a sorted set ring (score =
  member = emission wall-ms) trimmed to the trailing window, from which
  :func:`read_stats` derives ``observed_tps``.

Counters are **rebuildable** from ``event_buffer`` (INV-OBS-2): a Redis loss loses
no durable truth — ``manage.py rebuild_stream_stats`` reconstructs them.

**Failure stance.** Stats are observability, not a correctness gate. Every write
**fails open** (a Redis error never fails a delivery or a tick) — the same stance
as ``ws_connections``; the security fail-closed posture applies to credentials, not
counters. The read path surfaces ``health = "degraded"``/``"stale"`` when the data
is missing or old rather than erroring.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import redis
import structlog
from django.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from dataforge_engine.envelope import EnvelopeMapping

logger = structlog.get_logger("dataforge.delivery.stream_stats")

__all__ = [
    "TPS_WINDOW_S",
    "StreamStatsSnapshot",
    "read_stats",
    "record_delivered_batch",
    "stats_hash_key",
    "tps_ring_key",
    "write_rebuilt_stats",
]

# observed_tps sliding window (observability §5: "10 s sliding window"). Each
# delivered instance drops one member into the ring; read_stats counts the members
# inside the trailing window and divides by it.
TPS_WINDOW_S = 10

# The TPS ring is trimmed on every write and TTL'd so a stopped stream's ring drains
# instead of pinning memory; > the window with headroom for clock skew.
_TPS_TTL_S = 60

# Hash fields (the non-per-type ones). Per-type counts use the ``type:`` prefix so
# one HSCAN reconstructs by_event_type without a second key.
_F_TOTAL = "total_events"
_F_LAST_AT = "last_event_at"
_TYPE_PREFIX = "type:"

_HASH_KEY = "df:ws:{workspace_id}:stream:{stream_id}:stats"
_TPS_KEY = "df:ws:{workspace_id}:stream:{stream_id}:tps"


def stats_hash_key(*, workspace_id: str, stream_id: str) -> str:
    """The per-stream stats hash key (observability §5; workspace+stream labeled)."""
    return _HASH_KEY.format(workspace_id=workspace_id, stream_id=stream_id)


def tps_ring_key(*, workspace_id: str, stream_id: str) -> str:
    """The per-stream observed_tps sorted-set ring key (observability §5)."""
    return _TPS_KEY.format(workspace_id=workspace_id, stream_id=stream_id)


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def _parse_envelope_ts_ms(raw: str) -> int:
    """RFC-3339 envelope timestamp (``…Z`` / ``…+00:00``) → epoch-ms (best-effort).

    Used only to place a delivered instance in the observed_tps ring; a parse miss
    is silently skipped (the row still counts toward ``total_events``).
    """
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def record_delivered_batch(
    *,
    workspace_id: str,
    stream_id: str,
    envelopes: Sequence[EnvelopeMapping],
) -> None:
    """Increment the StreamStats counters for one durably-delivered batch (§5).

    Called by :meth:`BufferWriterChannel.deliver` **after** the ``event_buffer``
    transaction commits — every envelope here is a row REST will replay, so the
    tally reconciles with REST truth (the XCH exit criterion). Envelopes may be the
    internal (``_df`` present) or delivered shape; only the top-level ``event_type``
    / ``emitted_at`` keys are read, which are identical in both.

    One Redis pipeline per batch (INV-OBS-2 staleness budget): HINCRBY the total +
    per-type counts, HSET ``last_event_at`` to the batch tail's wall time, and add
    each instance to the trailing TPS ring (trimmed + TTL'd). Fails open — a Redis
    error is logged and swallowed so a delivery is never failed by stats (the
    counters are rebuildable, INV-OBS-2).
    """
    if not envelopes:
        return
    type_counts: dict[str, int] = {}
    ring_members: list[int] = []
    last_emitted_at = ""
    for env in envelopes:
        event_type = str(env.get("event_type", ""))
        if event_type:
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        emitted_at = env.get("emitted_at")
        if isinstance(emitted_at, str) and emitted_at:
            last_emitted_at = emitted_at  # batch is offset-ordered → tail is latest
            try:
                ring_members.append(_parse_envelope_ts_ms(emitted_at))
            except (ValueError, OSError):
                pass

    hkey = stats_hash_key(workspace_id=workspace_id, stream_id=stream_id)
    rkey = tps_ring_key(workspace_id=workspace_id, stream_id=stream_id)
    try:
        client = _redis()
        pipe = client.pipeline(transaction=False)
        pipe.hincrby(hkey, _F_TOTAL, len(envelopes))
        for event_type, count in type_counts.items():
            pipe.hincrby(hkey, f"{_TYPE_PREFIX}{event_type}", count)
        if last_emitted_at:
            pipe.hset(hkey, _F_LAST_AT, last_emitted_at)
        if ring_members:
            now_ms = ring_members[-1]
            # Member must be unique per *instance* (the score is the wall-ms; many
            # instances share a ms at high TPS, and batches repeat across calls). A
            # per-member UUID suffix makes each distinct so the ring's cardinality
            # equals the delivered count — divide by the window for observed_tps. The
            # score stays the ms so trim-by-score ages the window correctly.
            members = {f"{m}-{uuid.uuid4().hex}": float(m) for m in ring_members}
            pipe.zadd(rkey, members)
            # Trim everything older than the window so the ring stays O(window·tps).
            pipe.zremrangebyscore(rkey, 0, now_ms - TPS_WINDOW_S * 1000)
            pipe.expire(rkey, _TPS_TTL_S)
        pipe.execute()
    except redis.RedisError as exc:
        logger.warning(
            "stream_stats.record_degraded",
            stream_id=stream_id,
            rows=len(envelopes),
            error=str(exc),
        )


@dataclass(frozen=True)
class StreamStatsSnapshot:
    """A point-in-time read of one stream's Redis counters (observability §5).

    ``last_event_at`` is the RFC-3339 wall string stored at delivery (``None`` for a
    stream that has never delivered). ``observed_tps`` is the trailing-window rate.
    ``present`` is ``False`` when the hash is absent (no rows yet, or Redis lost the
    counters before a rebuild) — the read path renders that as ``health="degraded"``.
    """

    total_events: int
    by_event_type: dict[str, int]
    observed_tps: float
    last_event_at: str | None
    present: bool


def _decode(value: object) -> str:
    """Decode a Redis bytes/str field to ``str`` (the client returns bytes)."""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def read_stats(*, workspace_id: str, stream_id: str) -> StreamStatsSnapshot:
    """Read one stream's StreamStats counters from Redis (the §4.11.1 read path).

    Reads the hash (total + per-type + last_event_at) and counts the TPS ring inside
    the trailing window. Fails open: a Redis error returns an absent snapshot (the
    caller renders ``health="degraded"``) rather than failing the stats request.
    """
    hkey = stats_hash_key(workspace_id=workspace_id, stream_id=stream_id)
    rkey = tps_ring_key(workspace_id=workspace_id, stream_id=stream_id)
    try:
        client = _redis()
        raw = cast("dict[bytes, bytes]", client.hgetall(hkey))
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        window_count = cast(
            "int", client.zcount(rkey, now_ms - TPS_WINDOW_S * 1000, now_ms)
        )
    except redis.RedisError as exc:
        logger.warning("stream_stats.read_degraded", stream_id=stream_id, error=str(exc))
        return StreamStatsSnapshot(
            total_events=0,
            by_event_type={},
            observed_tps=0.0,
            last_event_at=None,
            present=False,
        )

    if not raw:
        return StreamStatsSnapshot(
            total_events=0,
            by_event_type={},
            observed_tps=0.0,
            last_event_at=None,
            present=False,
        )

    decoded = {_decode(k): _decode(v) for k, v in raw.items()}
    total = int(decoded.get(_F_TOTAL, "0"))
    last_at = decoded.get(_F_LAST_AT) or None
    by_type = {
        k[len(_TYPE_PREFIX) :]: int(v)
        for k, v in decoded.items()
        if k.startswith(_TYPE_PREFIX)
    }
    observed_tps = round(window_count / TPS_WINDOW_S, 1)
    return StreamStatsSnapshot(
        total_events=total,
        by_event_type=by_type,
        observed_tps=observed_tps,
        last_event_at=last_at,
        present=True,
    )


def write_rebuilt_stats(
    *,
    workspace_id: str,
    stream_id: str,
    total_events: int,
    by_event_type: dict[str, int],
    last_event_at: str | None,
    tps_ring_ms: Iterable[int] = (),
) -> None:
    """Replace a stream's Redis counters from a rebuilt tally (INV-OBS-2 rebuild).

    Used by ``manage.py rebuild_stream_stats``: DELETE the existing keys then write
    the recomputed total / per-type / last_event_at and (optionally) repopulate the
    TPS ring from recent rows so ``observed_tps`` recovers too. One pipeline; not
    fail-open (a rebuild is an explicit operation — surface a Redis error to the
    operator). ``by_event_type`` values are absolute counts (not increments).
    """
    hkey = stats_hash_key(workspace_id=workspace_id, stream_id=stream_id)
    rkey = tps_ring_key(workspace_id=workspace_id, stream_id=stream_id)
    client = _redis()
    pipe = client.pipeline(transaction=True)
    pipe.delete(hkey, rkey)
    fields: dict[str, str | int] = {_F_TOTAL: int(total_events)}
    for event_type, count in by_event_type.items():
        fields[f"{_TYPE_PREFIX}{event_type}"] = int(count)
    if last_event_at:
        fields[_F_LAST_AT] = last_event_at
    if fields:
        pipe.hset(hkey, mapping=fields)
    ring = list(tps_ring_ms)
    if ring:
        now_ms = max(ring)
        recent = [m for m in ring if m >= now_ms - TPS_WINDOW_S * 1000]
        if recent:
            members = {f"{m}-{uuid.uuid4().hex}": float(m) for m in recent}
            pipe.zadd(rkey, members)
            pipe.expire(rkey, _TPS_TTL_S)
    pipe.execute()
