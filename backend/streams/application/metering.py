"""Events/day metering + the admission-control budget (P11-07; PRD §7; scaling §5).

Two platform-protection concerns live here, both Phase 11 (the caps in
``streams.application.quotas`` were the synchronously-checkable subset; metering is
the rolling-usage half the api-spec §4.4 deferred to this phase):

1. **Events/day metering** — a per-workspace, UTC-day-bucketed Redis counter that
   the runner bumps as it emits (delivers) events. The counter feeds two reads:

   * the :class:`QuotaMeter` the console renders (consumed / cap / fraction); and
   * the exhaustion check the runner runs each tick — once a workspace's day total
     crosses its ``events_per_day`` cap, every running stream is system-paused to
     ``paused_quota`` (NEVER deleted — INV-TEN-5), audited
     ``streams.stream.system_paused {reason: quota}``, and a resume is rejected
     until consumption falls back under the cap (the T7 headroom guard).

   The counter is **per-workspace** (INV-OBS-3: one workspace's consumption never
   increments another's — exit criterion #7 / TEN §7.5). The Redis key embeds the
   workspace id and the UTC date; it expires two days after the bucket so stale
   buckets self-prune without a sweep. A durable copy lands in ``usage_counters``
   via a Celery flush (``tenancy.domain.models.UsageCounter``); the read falls back
   to that table when Redis is cold (best-effort cache, durable truth).

2. **Admission control** — the platform-capacity budget (scaling §5): the sum of
   provisioned ``target_tps`` across all *running* streams may not exceed 70 % of
   the measured platform capacity (3,500 eps at the GA 5 k ceiling). A ``start`` or
   ``target_tps`` raise that would push Σ over the budget is refused with
   ``503 service-unavailable`` + ``Retry-After: 300`` (RFC 9457). Running streams
   are NEVER touched — admission only gates *new* provisioned load.

Cardinality (M-3): no per-workspace/per-stream metric labels are emitted from here;
``df_quota_pauses_total{reason}`` carries the bounded ``reason`` label only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

import redis
import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

__all__ = [
    "ADMISSION_CAPACITY_EPS",
    "ADMISSION_HEADROOM_FRACTION",
    "ADMISSION_RETRY_AFTER_SECONDS",
    "AdmissionDenied",
    "QuotaMeter",
    "admission_budget_eps",
    "check_admission",
    "day_bucket_key",
    "events_consumed_today",
    "incr_events_today",
    "is_over_daily_quota",
    "quota_meter",
]

# --- Admission-control budget (scaling-strategy §5) --------------------------
# Measured platform capacity in events/s at the GA 5 k ceiling. A reviewable
# constant feeding the load-test ceiling report (P11-14); raised as the staircase
# rungs (scaling-strategy.md) are validated. Overridable per-env so staging can
# model a smaller machine without a code change.
ADMISSION_CAPACITY_EPS: int = getattr(settings, "DF_ADMISSION_CAPACITY_EPS", 3500)
# Headroom: provisioned Σ target_tps may use at most this fraction of capacity, so
# burst + late-arrival re-emission has room (scaling §5 "≤ 70 %").
ADMISSION_HEADROOM_FRACTION: float = getattr(settings, "DF_ADMISSION_HEADROOM_FRACTION", 0.70)
# RFC 9457 Retry-After for an admission-denied start/raise (seconds).
ADMISSION_RETRY_AFTER_SECONDS: int = 300


def admission_budget_eps() -> int:
    """The provisioned-TPS budget: ``capacity * headroom`` (scaling §5)."""
    return int(ADMISSION_CAPACITY_EPS * ADMISSION_HEADROOM_FRACTION)


class AdmissionDenied(Exception):
    """A start / TPS-raise would push Σ provisioned target_tps over the budget.

    Maps to ``503 service-unavailable`` + ``Retry-After: 300`` at the view (the
    platform is protecting itself; the request is retryable once load drains).
    """

    def __init__(self, *, provisioned: int, requested: int, budget: int) -> None:
        super().__init__(
            f"admitting {requested} more TPS would push provisioned "
            f"{provisioned}+{requested} over the platform budget of {budget} eps"
        )
        self.provisioned = provisioned
        self.requested = requested
        self.budget = budget
        self.retry_after = ADMISSION_RETRY_AFTER_SECONDS


# --- Events/day metering (Redis, UTC-day bucket) -----------------------------
# key: df:quota:events:{workspace_id}:{YYYY-MM-DD} → integer count, TTL 2 days.
_EVENTS_KEY = "df:quota:events:{workspace_id}:{day}"
# A bucket lives one day past its window so a near-midnight read still sees it; the
# next day's bucket is a fresh key. (No sweep needed — Redis expiry self-prunes.)
_BUCKET_TTL_SECONDS = 2 * 86_400


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


def _utc_day(at: datetime | None = None) -> date:
    """The UTC calendar day a counter buckets into (windows are UTC, PRD §7)."""
    moment = at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def day_bucket_key(workspace_id: UUID | str, *, at: datetime | None = None) -> str:
    """The Redis key for ``workspace_id``'s events counter on the UTC day of ``at``."""
    return _EVENTS_KEY.format(workspace_id=str(workspace_id), day=_utc_day(at).isoformat())


def incr_events_today(
    workspace_id: UUID | str, count: int, *, at: datetime | None = None
) -> int:
    """Bump ``workspace_id``'s UTC-day events counter by ``count`` (best-effort).

    Returns the post-increment day total (``0`` on a degraded cache — the caller
    treats a metering miss as "under quota", fail-open: a Redis outage must never
    pause a tenant's streams, mirroring the auth limiter's fail-open stance).
    The key is (re)stamped with a 2-day TTL so buckets self-prune.
    """
    if count <= 0:
        return 0
    key = day_bucket_key(workspace_id, at=at)
    try:
        client = _redis()
        pipe = client.pipeline()
        pipe.incrby(key, count)
        pipe.expire(key, _BUCKET_TTL_SECONDS)
        total = int(pipe.execute()[0])
        return total
    except redis.RedisError as exc:
        logger.warning("metering.incr_degraded", error=str(exc))
        return 0


def events_consumed_today(workspace_id: UUID | str, *, at: datetime | None = None) -> int:
    """``workspace_id``'s events consumed in the current UTC day.

    Reads the Redis bucket; falls back to the durable ``usage_counters`` row on a
    cache miss (the Celery flush keeps it current). Returns ``0`` if neither has a
    value (a workspace that has not emitted today).
    """
    key = day_bucket_key(workspace_id, at=at)
    try:
        raw = cast("bytes | None", _redis().get(key))
        if raw is not None:
            return int(raw)
    except redis.RedisError as exc:
        logger.warning("metering.read_degraded", error=str(exc))
    return _durable_events_today(workspace_id, at=at)


def _durable_events_today(workspace_id: UUID | str, *, at: datetime | None = None) -> int:
    """Durable fallback: today's ``events_delivered`` from ``usage_counters``."""
    from tenancy.domain.models import UsageCounter

    try:
        ws = workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    except (ValueError, TypeError):
        return 0
    row = UsageCounter.all_objects.filter(  # tenancy: own-workspace usage read by id
        workspace_id=ws, window_date=_utc_day(at)
    ).first()
    return int(row.events_delivered) if row is not None else 0


def is_over_daily_quota(
    workspace_id: UUID | str, cap: int, *, at: datetime | None = None
) -> bool:
    """Has ``workspace_id`` reached/exceeded its events/day cap on the UTC day?

    ``cap <= 0`` means unmetered (never over). A consumed total at or above the cap
    is "over" — exhaustion is inclusive so the cap is a true ceiling.
    """
    if cap <= 0:
        return False
    return events_consumed_today(workspace_id, at=at) >= cap


@dataclass(frozen=True)
class QuotaMeter:
    """The events/day consumption snapshot the console QuotaMeter renders (P11-09)."""

    consumed: int
    cap: int

    @property
    def fraction(self) -> float:
        """``consumed / cap`` clamped to ``[0, 1]`` (``0`` when unmetered)."""
        if self.cap <= 0:
            return 0.0
        return min(1.0, self.consumed / self.cap)

    @property
    def exhausted(self) -> bool:
        """True once consumption has reached the cap (the pause trigger)."""
        return self.cap > 0 and self.consumed >= self.cap


def quota_meter(workspace_id: UUID | str, *, at: datetime | None = None) -> QuotaMeter:
    """Build the :class:`QuotaMeter` for a workspace (console + recovery flows)."""
    from streams.application.quotas import events_per_day_cap

    return QuotaMeter(
        consumed=events_consumed_today(workspace_id, at=at),
        cap=events_per_day_cap(workspace_id),
    )


def check_admission(*, requested_tps: int, exclude_stream_id: UUID | Any | None = None) -> None:
    """Raise :class:`AdmissionDenied` if ``requested_tps`` exceeds the platform budget.

    ``requested_tps`` is the stream's target_tps being provisioned (a start) or the
    *increment* over its current provisioned value (a TPS raise). The check sums the
    provisioned ``target_tps`` of every currently-running/converging stream EXCEPT
    ``exclude_stream_id`` (so a TPS raise does not double-count the stream's existing
    provisioned load) and refuses if the sum + ``requested_tps`` would exceed the
    budget (scaling §5). Running streams are never affected — admission gates only the
    *new* provisioned load this command adds.
    """
    if requested_tps <= 0:
        return
    budget = admission_budget_eps()
    provisioned = _provisioned_running_tps(exclude_stream_id=exclude_stream_id)
    if provisioned + requested_tps > budget:
        raise AdmissionDenied(
            provisioned=provisioned, requested=requested_tps, budget=budget
        )


# Lifecycle states that hold provisioned platform load (a runner is, or is
# converging to, emitting for them). Mirrors quotas._IDLE_STATES' complement, but
# admission counts only states that actually provision capacity.
_PROVISIONING_STATES: frozenset[str] = frozenset(
    {"starting", "running", "resuming", "pausing"}
)


def _provisioned_running_tps(*, exclude_stream_id: UUID | Any | None = None) -> int:
    """Σ ``target_tps`` over all running/converging streams (cross-tenant platform sum).

    Admission is a platform-wide budget, so the sum spans every workspace's streams
    — read under ``platform_read_scope`` so the strict Class-T RLS policy admits all
    rows to the NOBYPASSRLS runtime role (read-only; no write here).
    """
    from django.db.models import Sum

    from streams.domain.models import Stream
    from tenancy.application.services import platform_read_scope

    with platform_read_scope():
        qs = Stream.all_objects.filter(  # tenancy: platform-wide admission sum (read-only)
            lifecycle_state__in=_PROVISIONING_STATES
        )
        if exclude_stream_id is not None:
            qs = qs.exclude(id=exclude_stream_id)
        total = qs.aggregate(total=Sum("target_tps"))["total"]
    return int(total or 0)
