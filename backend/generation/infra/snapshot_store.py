"""SnapshotSink port adapter + checkpoint persistence (database-schema §5.3-5.4).

* :class:`SnapshotSink` implements
  :class:`dataforge_engine.ports.SnapshotSink`: one ``entity_pool_snapshots`` row
  per (stream, shard, entity_type), payload = compressed JSONL (one pooled-entity
  image per line), stamped with the ``snapshot_epoch`` (= the upcoming
  ``checkpoint_seq``). Upsert is fenced on ``(fencing_token, snapshot_epoch)``
  (§5.4 commit-marker rule).
* :func:`write_checkpoint` persists the engine checkpoint codec blob (§9.1) to
  ``stream_checkpoints`` at batch finalization, fenced on
  ``(fencing_token, checkpoint_seq)`` (§5.3 conditional write). The blob is the
  canonical-JSON string from ``encode_to_json`` (codec §9.1), compressed.

The write ordering is the commit-marker rule: snapshots first (each stamped with
the upcoming ``checkpoint_seq``), then the checkpoint row last (the commit
marker). Restore loads the checkpoint row, then snapshots
``WHERE snapshot_epoch = checkpoint_seq`` (lease-driven pause/resume is Phase 5-6;
the persistence ships now for batch finalization).

All writes go through the Django ``default`` connection (the runtime
``dataforge_app`` NOBYPASSRLS role) so RLS applies; the caller arms the workspace
context. ``workspace_id`` is denormalized (C-8).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from generation.infra.compression import compress

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from dataforge_engine.envelope.types import JSONValue

__all__ = ["SnapshotSink", "write_checkpoint"]


def _json_default(value: Any) -> str:
    """Render a ``Decimal`` pool attribute as its literal digit string (S-6).

    Pool images carry generated ``Decimal`` values (e.g. prices); the snapshot
    JSONL keeps them as their exact decimal digits so a restored pool is identical.
    """
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to the pool snapshot")


class SnapshotSink:
    """Concrete :class:`dataforge_engine.ports.SnapshotSink` over Postgres (§5.4).

    Bound to one (workspace, stream, shard) at construction.
    """

    def __init__(
        self, *, workspace_id: str, stream_id: str, shard_id: int, fencing_token: int = 0
    ) -> None:
        self._workspace_id = workspace_id
        self._stream_id = stream_id
        self._shard_id = shard_id
        self._fencing_token = fencing_token

    def write_pool_image(
        self,
        *,
        entity_type: str,
        snapshot_epoch: int,
        records: Iterable[Mapping[str, JSONValue]],
    ) -> None:
        """Persist a full pool image for one entity type at one checkpoint epoch."""
        from generation.domain.models import EntityPoolSnapshot

        lines: list[str] = []
        count = 0
        for record in records:
            lines.append(
                json.dumps(
                    record, separators=(",", ":"), sort_keys=True, default=_json_default
                )
            )
            count += 1
        payload = compress(("\n".join(lines)).encode("utf-8"))
        # tenancy: unscoped — fenced upsert keyed by (stream, shard, type); RLS applies at DB.
        EntityPoolSnapshot.all_objects.update_or_create(
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            entity_type=entity_type,
            defaults={
                "workspace_id": self._workspace_id,
                "snapshot_epoch": snapshot_epoch,
                "fencing_token": self._fencing_token,
                "payload": payload,
                "entity_count": count,
                "updated_at": datetime.now().astimezone(),
            },
        )

    def load_pool_image(
        self, *, entity_type: str, snapshot_epoch: int
    ) -> Iterable[Mapping[str, JSONValue]]:
        """Load the persisted image for restore (behavior-engine §9.3 step 2)."""
        from generation.domain.models import EntityPoolSnapshot
        from generation.infra.compression import decompress

        # tenancy: unscoped — restore keyed by (stream, shard, type); RLS applies at DB.
        row = EntityPoolSnapshot.all_objects.filter(
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            entity_type=entity_type,
            snapshot_epoch=snapshot_epoch,
        ).first()
        if row is None:
            return []
        text = decompress(bytes(row.payload)).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line]


def write_checkpoint(
    *,
    workspace_id: str,
    stream_id: str,
    shard_id: int,
    checkpoint_seq: int,
    fencing_token: int,
    blob_json: str,
    last_sequence_no: int,
    virtual_clock_at: datetime,
) -> None:
    """Persist the engine checkpoint blob to ``stream_checkpoints`` (§5.3, §9.1).

    The blob is the canonical-JSON string from ``encode_to_json`` (codec §9.1),
    compressed into the ``state`` ``bytea``. Single-row upsert keyed by
    (stream, shard); the §5.3 fenced condition (token/seq monotonicity) is the
    Phase 5-6 lease seam, applied here as a guard so a stale write is a no-op.
    """
    from generation.domain.models import StreamCheckpoint

    state = compress(blob_json.encode("utf-8"))
    # tenancy: unscoped — single-row lookup keyed by (stream, shard); RLS applies at DB.
    existing = StreamCheckpoint.all_objects.filter(
        stream_id=stream_id, shard_id=shard_id
    ).first()
    if existing is not None and (
        existing.fencing_token > fencing_token or existing.checkpoint_seq >= checkpoint_seq
    ):
        return  # stale token or replayed seq — the conditional write is a no-op (§5.3)
    # tenancy: unscoped — fenced single-row upsert keyed by (stream, shard); RLS applies.
    StreamCheckpoint.all_objects.update_or_create(
        stream_id=stream_id,
        shard_id=shard_id,
        defaults={
            "workspace_id": workspace_id,
            "checkpoint_seq": checkpoint_seq,
            "fencing_token": fencing_token,
            "state": state,
            "state_format": 1,
            "last_sequence_no": last_sequence_no,
            "virtual_clock_at": virtual_clock_at,
            "updated_at": datetime.now().astimezone(),
        },
    )
