"""``manage.py rebuild_stream_stats`` — reconstruct StreamStats from ``event_buffer``.

The recovery path for INV-OBS-2: the Redis counters are *rebuildable*, so a Redis
loss loses no durable truth. This command recomputes each stream's
``total_events`` / ``by_event_type`` / ``last_event_at`` (and repopulates the
observed_tps ring from recent rows) directly from the durable ``event_buffer`` and
overwrites the Redis hash — the same tally the buffer-writer sink would have
accumulated, since the buffer-writer counts exactly the rows it commits.

Usage::

    manage.py rebuild_stream_stats                 # every stream with buffered rows
    manage.py rebuild_stream_stats --stream-id <uuid>

Each stream is rebuilt under its own workspace context so the ``event_buffer`` read
is RLS-scoped to the owning tenant (INV-OBS-3) — the runtime NOBYPASSRLS role.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from delivery.infra import buffer_stats, stream_stats
from streams.domain.models import Stream
from tenancy.application.services import worker_workspace_scope

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    help = (
        "Rebuild the Redis StreamStats counters from event_buffer (INV-OBS-2 "
        "rebuildable; the Redis-loss recovery path)."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--stream-id",
            dest="stream_id",
            default=None,
            help="Rebuild only this stream (UUID); default rebuilds every stream.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        targets = self._resolve_targets(options.get("stream_id"))
        if not targets:
            self.stdout.write("No streams to rebuild.")
            return
        for workspace_id, stream_id in targets:
            self._rebuild_one(workspace_id=workspace_id, stream_id=stream_id)

    def _resolve_targets(self, raw_stream_id: str | None) -> list[tuple[str, str]]:
        """Return ``(workspace_id, stream_id)`` pairs to rebuild (unscoped read).

        Streams are resolved through the unscoped manager (a platform maintenance
        command, not a tenant request); each rebuild then arms the stream's own
        workspace before touching ``event_buffer`` so the row read is RLS-scoped.
        """
        if raw_stream_id is not None:
            try:
                sid = uuid.UUID(str(raw_stream_id))
            except (ValueError, TypeError) as exc:
                raise CommandError(f"Invalid --stream-id: {raw_stream_id!r}") from exc
            row = Stream.all_objects.filter(id=sid).values_list(
                "workspace_id", "id"
            ).first()
            if row is None:
                raise CommandError(f"No stream {sid}")
            return [(str(row[0]), str(row[1]))]
        return [
            (str(ws), str(sid))
            for ws, sid in Stream.all_objects.all().values_list("workspace_id", "id")
        ]

    def _rebuild_one(self, *, workspace_id: str, stream_id: str) -> None:
        with worker_workspace_scope(uuid.UUID(workspace_id)):
            tally = buffer_stats.tally_from_buffer(
                stream_id=stream_id, tps_window_s=stream_stats.TPS_WINDOW_S
            )
        stream_stats.write_rebuilt_stats(
            workspace_id=workspace_id,
            stream_id=stream_id,
            total_events=tally.total_events,
            by_event_type=tally.by_event_type,
            last_event_at=tally.last_event_at,
            tps_ring_ms=tally.recent_emitted_ms,
        )
        self.stdout.write(
            f"{stream_id}: rebuilt total={tally.total_events} "
            f"types={len(tally.by_event_type)} last_event_at={tally.last_event_at}"
        )
