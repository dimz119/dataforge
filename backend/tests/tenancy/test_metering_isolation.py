"""Tenant metering isolation — INV-OBS-3 / phase-11 exit #7 (PERMANENT).

Exit criterion #7: *"Tenant metering isolation: one workspace's consumption never
increments another's counters."* The events/day meter is per-workspace by key
construction (``df:quota:events:{workspace_id}:{YYYY-MM-DD}``); this permanent probe
proves that property end to end against live Redis — a cross-tenant metering breach
would let one tenant's load exhaust (or hide under) another's quota, the most direct
quota-isolation failure.

The assertions:

* incrementing workspace A's counter leaves workspace B's read at zero (no bleed);
* A's and B's bucket keys are distinct (the structural root of isolation);
* the over-quota decision for A is computed from A's own consumption only — A going
  over its cap does NOT flip B's ``is_over_daily_quota``;
* the day bucket is UTC-dated, so A's counter on day D never collides with B's on D.

Lives in the permanent TEN suite (testing §7.5). Uses live Redis (the meter's real
store); each test cleans its own keys so the suite is order-independent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import redis
from django.conf import settings

from streams.application import metering

# A pinned UTC instant so the bucket date is deterministic and the test never races a
# real midnight rollover.
_AT = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def two_workspace_meters() -> Iterator[tuple[str, str]]:
    """Two fresh workspace ids with their Redis day-buckets cleaned before + after."""
    ws_a, ws_b = str(uuid4()), str(uuid4())
    client = redis.Redis.from_url(settings.REDIS_URL)
    keys = [metering.day_bucket_key(ws_a, at=_AT), metering.day_bucket_key(ws_b, at=_AT)]
    client.delete(*keys)
    yield ws_a, ws_b
    client.delete(*keys)


def test_increment_does_not_bleed_into_another_workspace(
    two_workspace_meters: tuple[str, str], db: object
) -> None:
    """A's consumption never appears in B's counter (INV-OBS-3; exit #7)."""
    ws_a, ws_b = two_workspace_meters
    total = metering.incr_events_today(ws_a, 100, at=_AT)
    metering.incr_events_today(ws_a, 250, at=_AT)
    assert total == 100  # post-increment running total of the first bump
    assert metering.events_consumed_today(ws_a, at=_AT) == 350
    # B never received a write — its read is zero, not A's 350.
    assert metering.events_consumed_today(ws_b, at=_AT) == 0


def test_bucket_keys_are_per_workspace_distinct(two_workspace_meters: tuple[str, str]) -> None:
    """A's and B's day-bucket keys differ (the structural basis for isolation)."""
    ws_a, ws_b = two_workspace_meters
    key_a = metering.day_bucket_key(ws_a, at=_AT)
    key_b = metering.day_bucket_key(ws_b, at=_AT)
    assert key_a != key_b
    assert ws_a in key_a and ws_b in key_b
    assert key_a.startswith("df:quota:events:") and "2026-06-22" in key_a


def test_over_quota_for_one_workspace_does_not_flip_another(
    two_workspace_meters: tuple[str, str], db: object
) -> None:
    """A exhausting its cap does NOT make B over-quota (the decision is per-tenant)."""
    ws_a, ws_b = two_workspace_meters
    cap = 1_000
    metering.incr_events_today(ws_a, cap + 5, at=_AT)  # A is over its cap
    assert metering.is_over_daily_quota(ws_a, cap, at=_AT) is True
    # B consumed nothing — it is NOT over the same cap (A's load didn't count for B).
    assert metering.is_over_daily_quota(ws_b, cap, at=_AT) is False
    assert metering.events_consumed_today(ws_b, at=_AT) == 0


def test_quota_meter_snapshot_is_isolated(
    two_workspace_meters: tuple[str, str], db: object
) -> None:
    """The console :class:`QuotaMeter` reflects only the workspace's own consumption."""
    ws_a, ws_b = two_workspace_meters
    metering.incr_events_today(ws_a, 400, at=_AT)
    meter_a = metering.quota_meter(ws_a, at=_AT)
    meter_b = metering.quota_meter(ws_b, at=_AT)
    assert meter_a.consumed == 400
    assert meter_b.consumed == 0
    # Free cap is 1,000,000; A's fraction reflects 400/cap, B's is 0.
    assert meter_a.fraction > 0.0
    assert meter_b.fraction == 0.0
