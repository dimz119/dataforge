"""``LateArrivalBuffer`` host — the durable late-arrival buffer (chaos-engine §6).

The Postgres-backed implementation of the engine's ``LateBuffer`` port (the engine
declares the protocol; this app supplies the persistence — it touches Postgres, so
it lives here, NOT in ``dataforge_engine``, keeping engine purity, BE-ENG-1).

Lifecycle responsibilities (§6.3, every case):

* ``insert(entry)`` — the in-line port call from the ``late_arriving`` stage:
  buffers the descriptor in memory for this tick.
* ``schedule()`` — persists the tick's buffered entries as ``pending`` rows
  (INV-CHA-5: durable, survives pause + failover because it is Postgres state).
* ``take_due(now)`` — the scheduler (§6.2): claims ``pending`` rows with
  ``due_at <= now`` (``FOR UPDATE SKIP LOCKED`` on Postgres), re-emits each with the
  OLD ``occurred_at`` and a NEW ``emitted_at = now`` (INV-CHA-6), flips the row to
  ``emitted``, and finalizes the injection record (``outcome``, realized delay).
* ``flush_pending()`` / ``discard_pending()`` — the stop ``OnStopPolicy`` (§6.3):
  ``flush`` publishes every pending entry now (ignoring ``due_at``, ``outcome:
  flushed``); ``discard`` (default) marks them ``discarded`` (``outcome: discarded``).

Failover is free: because pending rows are durable Postgres state, a FRESH buffer
instance under the new lease holder's ``take_due`` picks up the pending entries on
its first tick — no in-memory hand-off (§6.3 failover row).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from django.db import connection, transaction

from chaos.domain.models import (
    BUFFER_DISCARDED,
    BUFFER_EMITTED,
    BUFFER_PENDING,
    ChaosInjection,
    LateArrivalBufferEntry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["LateArrivalBuffer"]

TAKE_LIMIT_DEFAULT = 500  # §6.2 scheduler page (paced re-emission)


def _fmt_emitted(dt: datetime) -> str:
    """Wall ``datetime`` → the envelope's RFC-3339 ``emitted_at`` string shape."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class LateArrivalBuffer:
    """Durable per-(stream, shard) late-arrival buffer (§6). One per worker setup.

    ``publish`` is the runner's keyed-publish callback (``list[envelope] -> int``);
    re-emissions flow through it exactly like in-line instances. ``k`` is the
    stream's ``speed_multiplier`` (for the realized-delay accounting).
    """

    def __init__(
        self,
        *,
        workspace_id: str,
        stream_id: str,
        shard_id: int,
        publish: Any,
        speed_multiplier: float = 1.0,
    ) -> None:
        self._workspace_id = workspace_id
        self._stream_id = stream_id
        self._shard_id = shard_id
        self._publish = publish
        self._k = speed_multiplier if speed_multiplier > 0 else 1.0
        self._pending_inserts: list[dict[str, Any]] = []

    # -- the engine LateBuffer port (in-line, per stage call) --------------------

    def insert(self, entry: object) -> None:
        """Buffer one :class:`ScheduledEntry` descriptor for this tick (§6.1)."""
        self._pending_inserts.append(dict(cast("dict[str, Any]", entry)))

    # -- runner seam: persist the tick's selections ------------------------------

    def schedule(self, entries: Iterable[dict[str, Any]] | None = None) -> int:
        """Persist buffered (or supplied) entries as ``pending`` rows (durable §6.1).

        Idempotent on the deterministic ``injection_id`` (one buffer row per late
        injection): a re-run of the same tick (tick retry) re-derives the same
        ``injection_id`` so ``ignore_conflicts`` collapses the re-insert (CR-7).
        """
        items = list(entries) if entries is not None else self._pending_inserts
        if not items:
            self._pending_inserts = []
            return 0
        rows = [
            LateArrivalBufferEntry(
                id=item["injection_id"],
                workspace_id=self._workspace_id,
                stream_id=item["stream_id"],
                shard_id=item["shard_id"],
                injection_id=item["injection_id"],
                event_id=item["event_id"],
                envelope=item["envelope"],
                due_at=item["due_at"],
                state=BUFFER_PENDING,
            )
            for item in items
        ]
        LateArrivalBufferEntry.objects.bulk_create(rows, ignore_conflicts=True)
        self._pending_inserts = []
        return len(rows)

    # -- the scheduler (§6.2) ----------------------------------------------------

    def take_due(self, now: datetime, limit: int = TAKE_LIMIT_DEFAULT) -> int:
        """Publish + finalize ``pending`` rows due at/before ``now`` (§6.2).

        Returns the count re-emitted. Publish-then-flip (§6.3): the row is marked
        ``emitted`` and the injection record finalized in the SAME transaction as
        the durable state flip; a crash between publish and flip re-emits at most
        once on recovery — inside the at-least-once contract (event-model §6).
        """
        return self._drain(now=now, limit=limit, ignore_due=False, outcome="emitted")

    # -- stop OnStopPolicy (§6.3) ------------------------------------------------

    def flush_pending(self, now: datetime) -> int:
        """``flush``: publish EVERY pending entry now, ignoring ``due_at`` (§6.3)."""
        return self._drain(now=now, limit=None, ignore_due=True, outcome="flushed")

    def discard_pending(self, now: datetime) -> int:
        """``discard`` (default): mark every pending entry ``discarded`` (§6.3)."""
        qs = self._pending_qs()
        ids = list(qs.values_list("id", flat=True))
        if not ids:
            return 0
        with transaction.atomic():
            for entry in self._lock_by_ids(ids):
                self._finalize_injection(entry, realized_dt=None, outcome=BUFFER_DISCARDED)
            LateArrivalBufferEntry.objects.filter(id__in=ids).update(
                state=BUFFER_DISCARDED, resolved_at=now
            )
        return len(ids)

    # -- internals ---------------------------------------------------------------

    def _drain(
        self, *, now: datetime, limit: int | None, ignore_due: bool, outcome: str
    ) -> int:
        qs = self._pending_qs()
        if not ignore_due:
            qs = qs.filter(due_at__lte=now)
        qs = qs.order_by("due_at")
        if limit is not None:
            qs = qs[:limit]
        ids = list(qs.values_list("id", flat=True))
        if not ids:
            return 0
        count = 0
        with transaction.atomic():
            for entry in self._lock_by_ids(ids):
                envelope = dict(entry.envelope)
                envelope["emitted_at"] = _fmt_emitted(now)
                self._publish([envelope])
                self._finalize_injection(entry, realized_dt=now, outcome=outcome)
                entry.state = BUFFER_EMITTED
                entry.resolved_at = now
                entry.save(update_fields=["state", "resolved_at"])
                count += 1
        return count

    def _pending_qs(self) -> Any:
        """The scoped ``pending`` rows for this (stream, shard) — the §6.2 scan."""
        return LateArrivalBufferEntry.objects.filter(
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            state=BUFFER_PENDING,
        )

    def _finalize_injection(
        self, entry: LateArrivalBufferEntry, *, realized_dt: datetime | None, outcome: str
    ) -> None:
        """Stamp ``outcome`` + ``realized_wall_delay_ms`` on the injection (§6.4)."""
        try:
            injection = ChaosInjection.objects.get(injection_id=entry.injection_id)
        except ChaosInjection.DoesNotExist:
            return
        details = dict(injection.details)
        details["outcome"] = outcome
        if realized_dt is not None:
            delta_ms = int(
                (realized_dt - injection.canonical_emitted_at).total_seconds() * 1000
            )
            # Realized wall delay ≥ simulated_delay / k; clamp negatives to 0.
            details["realized_wall_delay_ms"] = max(0, delta_ms)
        injection.details = details
        injection.save(update_fields=["details"])

    def _lock_by_ids(self, ids: Sequence[Any]) -> list[LateArrivalBufferEntry]:
        """The claimed rows in ``due_at`` order, locked ``FOR UPDATE SKIP LOCKED``
        on Postgres (§6.2) — drains overdue backlogs oldest-first.
        """
        qs = LateArrivalBufferEntry.objects.filter(id__in=ids).order_by("due_at")
        if connection.vendor == "postgresql":
            qs = qs.select_for_update(skip_locked=True)
        return list(qs)
