"""Backfill quota caps enforced at command time (PRD §7; api-spec §4.10.1).

Quota *metering/enforcement infra* lands in Phase 11; until then a single implicit
Free-tier quota applies per workspace (PRD §7: "what exists until then is a single
implicit Free-tier quota row per workspace with these limits enforced"). The
backfill caps are:

| Plan       | max simulated days | max events / batch |
|------------|--------------------|--------------------|
| Free       | 7                  | 1,000,000          |
| Classroom  | 30                 | 5,000,000          |
| Pro        | 90                 | 20,000,000         |

This module exposes the cap lookup (Free by default) and the two pure checks the
dataset command runs before any row is written. The seam
:func:`backfill_caps_for` reads a future ``workspace_quotas`` row when the quotas
table exists (Phase 11); today it returns the Free caps.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BackfillCaps", "QuotaExceededError", "backfill_caps_for"]


@dataclass(frozen=True)
class BackfillCaps:
    """The two backfill ceilings for a plan (PRD §7)."""

    max_simulated_days: int
    max_events: int


# Plan caps (PRD §7). Free is the implicit default until Phase 11 metering.
FREE = BackfillCaps(max_simulated_days=7, max_events=1_000_000)
CLASSROOM = BackfillCaps(max_simulated_days=30, max_events=5_000_000)
PRO = BackfillCaps(max_simulated_days=90, max_events=20_000_000)

_PLAN_CAPS = {"free": FREE, "classroom": CLASSROOM, "pro": PRO}


class QuotaExceededError(Exception):
    """A backfill request exceeds a plan cap. Carries the breached quota."""

    def __init__(self, *, quota: str, limit: int, requested: int) -> None:
        super().__init__(
            f"{quota} {requested} exceeds the plan limit of {limit} (PRD §7)"
        )
        self.quota = quota
        self.limit = limit
        self.requested = requested


def backfill_caps_for(workspace_id: str) -> BackfillCaps:
    """The backfill caps for a workspace.

    Phase 11 reads the plan from ``workspace_quotas``; until then every workspace
    is implicitly Free (PRD §7). The argument is kept so the call site is
    Phase-11-ready without a signature change.
    """
    return FREE


def enforce_backfill(
    *, caps: BackfillCaps, simulated_days: int, estimated_events: int
) -> None:
    """Raise :class:`QuotaExceededError` if either cap is breached (api §4.10.1).

    The event estimate derives from the manifest dry-run ``mean_events_per_session``
    (behavior-engine); the caller computes it before calling.
    """
    if simulated_days > caps.max_simulated_days:
        raise QuotaExceededError(
            quota="simulated_days",
            limit=caps.max_simulated_days,
            requested=simulated_days,
        )
    if estimated_events > caps.max_events:
        raise QuotaExceededError(
            quota="estimated_events", limit=caps.max_events, requested=estimated_events
        )
