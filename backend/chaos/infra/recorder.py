"""``InjectionRecorder`` — the Postgres answer-key sink (chaos-engine §7.1).

The Django-side implementation of the engine's ``Recorder`` port: persists each
:class:`~dataforge_engine.chaos.InjectionRecord` to ``chaos_injections`` BEFORE the
affected instance is published/buffered/suppressed (INV-CHA-4). Idempotent on the
deterministic ``injection_id`` (CR-7): a tick retry re-derives the same id, so
``ignore_conflicts`` collapses the re-insert and never double-counts.

Lives in the chaos app (touches Postgres) — the engine stays pure (BE-ENG-1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chaos.domain.models import ChaosInjection

if TYPE_CHECKING:
    from dataforge_engine.chaos import InjectionRecord

__all__ = ["InjectionRecorder"]


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class InjectionRecorder:
    """Concrete :class:`dataforge_engine.chaos.Recorder` over Postgres (§7.1).

    Buffers per-tick records and flushes them as one idempotent batch so the
    record-before-effect ordering holds at the tick boundary (CR-7). Call
    :meth:`flush` after the pipeline runs and BEFORE publish/extraction.
    """

    def __init__(self) -> None:
        self._buffer: list[ChaosInjection] = []
        self._seen: set[str] = set()
        self.recorded_total = 0

    def record(self, injection: InjectionRecord) -> None:
        """Stage the injection (idempotent on ``injection_id`` within the tick)."""
        injection_id = injection["injection_id"]
        if injection_id in self._seen:
            return
        self._seen.add(injection_id)
        self._buffer.append(
            ChaosInjection(
                injection_id=injection_id,
                workspace_id=injection["workspace_id"],
                stream_id=injection["stream_id"],
                shard_id=injection["shard_id"],
                mode=injection["mode"],
                event_id=injection["event_id"],
                sequence_no=injection["sequence_no"],
                occurred_at=_parse_ts(injection["occurred_at"]),
                canonical_emitted_at=_parse_ts(injection["canonical_emitted_at"]),
                details=dict(injection["details"]),
            )
        )

    def flush(self) -> int:
        """Persist the staged records as one idempotent batch; clears the buffer."""
        if not self._buffer:
            return 0
        rows = self._buffer
        ChaosInjection.objects.bulk_create(rows, ignore_conflicts=True)
        count = len(rows)
        self.recorded_total += count
        self._buffer = []
        self._seen = set()
        return count

    @property
    def pending(self) -> list[Any]:
        """The staged-but-unflushed records (for tests / assertions)."""
        return list(self._buffer)
