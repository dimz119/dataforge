"""Runner checkpoint store — the §8.2 fenced conditional write (backend-architecture
§8.2 checkpoint row, §8.4 cadence; behavior-engine §9).

The shard worker persists the engine checkpoint codec blob + pool snapshots every
30 s (§8.4) and at finalize/pause. Unlike the batch-finalization helper
(``generation.infra.snapshot_store.write_checkpoint``, which silently no-ops a
stale write at batch time), the *runtime* checkpoint is a **fenced conditional
write**: the §8.2 enforcement point::

    UPDATE stream_checkpoints
       SET state = …, fencing_token = mine, checkpoint_seq = seq, …
     WHERE stream_id = … AND shard_id = …
       AND fencing_token <= mine

A zombie's stale token matches no row (``rows_affected == 0``) and raises
:class:`runner.fencing.FencingError` — the worker is fenced, the supervisor tears
it down, and durable state can never roll back (INV-STR-2). On first checkpoint
(no row yet) an ``INSERT`` lands.

Restore (failover takeover, §8.5) loads the most-recent checkpoint row and its
pool images, rehydrates the engine via the codec, and the worker regenerates the
≤ 30 s gap into the idempotent ledger/Kafka sinks.

This module is a Django host seam (it owns the ORM access the engine must not), so
it does the blocking DB work behind ``asyncio.to_thread`` for the async tick. The
engine codec stays pure (``encode_checkpoint`` / ``restore_checkpoint``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import connection

from dataforge_engine.behavior import (
    encode_checkpoint,
    restore_checkpoint,
)
from runner.fencing import FencingError, enforce_conditional_write

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dataforge_engine.behavior import Shard

__all__ = ["CheckpointStore", "RestoredCheckpoint"]


def _json_default(value: Any) -> str:
    """Render a ``Decimal`` (remembered memory value) as its literal digits (S-6)."""
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__} to the checkpoint blob")


def _uuid_param(value: str) -> str:
    """Format a UUID for the active DB's raw-SQL binding (mirrors LedgerSink).

    Postgres' ``uuid`` type accepts the canonical dashed form; the SQLite unit lane
    stores ``UUIDField`` as 32-char hex (Django's converter, which raw SQL bypasses),
    so a raw ``WHERE stream_id = %s`` must use the 32-hex form to match the ORM row.
    """
    import uuid as _uuid

    parsed = _uuid.UUID(str(value))
    return str(parsed) if connection.vendor == "postgresql" else parsed.hex


@dataclass(frozen=True)
class RestoredCheckpoint:
    """A loaded checkpoint row (the takeover input, §8.5). ``None`` → first start."""

    checkpoint_seq: int
    fencing_token: int
    last_sequence_no: int
    blob: dict[str, Any]


class CheckpointStore:
    """Fenced runtime checkpoint persistence for one (workspace, stream, shard).

    Bound at construction; the worker passes its live ``fencing_token`` per save so
    a token bump on re-acquire is honoured. Blocking ORM/SQL runs in a thread so
    the asyncio tick never blocks the event loop.
    """

    def __init__(self, *, workspace_id: str, stream_id: str, shard_id: int) -> None:
        self._workspace_id = workspace_id
        self._stream_id = stream_id
        self._shard_id = shard_id

    def _ws_scope(self) -> Any:
        """Arm the row's workspace so RLS admits the checkpoint/snapshot SQL (§4.2).

        The runner runs as the NOBYPASSRLS runtime role; the checkpoint + pool-image
        reads/writes are Class T rows whose USING/WITH CHECK require the row's
        workspace armed as ``app.workspace_id``. ``worker_workspace_scope`` opens one
        ``transaction.atomic()`` and ``SET LOCAL``s the GUC on the ORM connection —
        which is the same connection the raw checkpoint SQL uses in this thread.
        """
        import uuid as _uuid

        from tenancy.application.services import worker_workspace_scope

        return worker_workspace_scope(_uuid.UUID(self._workspace_id))

    # -- save (§8.4 cadence; §8.2 fenced conditional write) -----------------------

    async def save(
        self,
        shard: Shard,
        *,
        fencing_token: int,
        checkpoint_seq: int,
        config_sha256: str = "",
    ) -> None:
        """Persist snapshots then the fenced checkpoint row. Raise if fenced.

        Commit-marker order (database-schema §5.4): pool images first (stamped with
        the upcoming ``checkpoint_seq``), then the checkpoint row last as the commit
        marker. The checkpoint write is the §8.2 conditional write — a stale token
        raises :class:`FencingError`.
        """
        blob = encode_checkpoint(
            shard, checkpoint_seq=checkpoint_seq, config_sha256=config_sha256
        )
        blob_json = json.dumps(
            blob, separators=(",", ":"), sort_keys=True, default=_json_default
        )
        last_sequence_no = shard.sequence.last
        virtual_at = shard.clock.instant_for(shard.clock.frontier_us)
        await asyncio.to_thread(
            self._save_sync,
            shard=shard,
            fencing_token=fencing_token,
            checkpoint_seq=checkpoint_seq,
            blob_json=blob_json,
            last_sequence_no=last_sequence_no,
            virtual_at=virtual_at,
        )

    def _save_sync(
        self,
        *,
        shard: Shard,
        fencing_token: int,
        checkpoint_seq: int,
        blob_json: str,
        last_sequence_no: int,
        virtual_at: datetime,
    ) -> None:
        from generation.infra.compression import compress
        from generation.infra.snapshot_store import SnapshotSink

        state = compress(blob_json.encode("utf-8"))
        snapshot = SnapshotSink(
            workspace_id=self._workspace_id,
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            fencing_token=fencing_token,
        )
        with self._ws_scope():
            # Commit-marker rule: snapshots first (stamped with this seq)…
            ir = shard.ir
            for entity_type in ir.entity_order:
                pool = shard.pools.pool(entity_type)
                records = (pool.records[key].snapshot_json() for key in pool.records)
                snapshot.write_pool_image(
                    entity_type=entity_type,
                    snapshot_epoch=checkpoint_seq,
                    records=records,
                )
            # …then the fenced checkpoint row last (the commit marker, §8.2).
            self._upsert_fenced(
                fencing_token=fencing_token,
                checkpoint_seq=checkpoint_seq,
                state=state,
                last_sequence_no=last_sequence_no,
                virtual_at=virtual_at,
            )

    def _upsert_fenced(
        self,
        *,
        fencing_token: int,
        checkpoint_seq: int,
        state: bytes,
        last_sequence_no: int,
        virtual_at: datetime,
    ) -> None:
        """The §8.2 conditional write: ``WHERE fencing_token <= mine``; raise if 0.

        Tries the guarded UPDATE first; if it matches a row, the write landed. If it
        matched zero rows, either no row exists yet (first checkpoint → INSERT) or a
        strictly-greater token already won (a takeover → fenced). The INSERT's unique
        ``(stream_id, shard_id)`` distinguishes the two: a conflict means a fresher
        holder inserted concurrently, so we are fenced.
        """
        now = datetime.now().astimezone()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE stream_checkpoints "
                "SET checkpoint_seq = %s, fencing_token = %s, state = %s, "
                "    state_format = 1, last_sequence_no = %s, virtual_clock_at = %s, "
                "    updated_at = %s "
                "WHERE stream_id = %s AND shard_id = %s AND fencing_token <= %s",
                [
                    checkpoint_seq,
                    fencing_token,
                    state,
                    last_sequence_no,
                    virtual_at,
                    now,
                    _uuid_param(self._stream_id),
                    self._shard_id,
                    fencing_token,
                ],
            )
            updated = cursor.rowcount
            if updated > 0:
                return
            # Zero rows: distinguish "no row yet" (INSERT) from "fenced" (a greater
            # token already present). A guarded INSERT … no-op-on-conflict lets a
            # concurrent fresher holder win without us clobbering it.
            inserted = self._try_insert(
                cursor,
                fencing_token=fencing_token,
                checkpoint_seq=checkpoint_seq,
                state=state,
                last_sequence_no=last_sequence_no,
                virtual_at=virtual_at,
                now=now,
            )
        # Outside the cursor: if neither updated nor inserted, we are fenced (§8.2).
        enforce_conditional_write(
            updated + inserted,
            stream_id=self._stream_id,
            shard_id=self._shard_id,
            my_token=fencing_token,
            surface="checkpoint",
        )

    def _try_insert(
        self,
        cursor: Any,
        *,
        fencing_token: int,
        checkpoint_seq: int,
        state: bytes,
        last_sequence_no: int,
        virtual_at: datetime,
        now: datetime,
    ) -> int:
        is_pg = connection.vendor == "postgresql"
        conflict = (
            "ON CONFLICT (stream_id, shard_id) DO NOTHING"
            if is_pg
            else "OR IGNORE"
        )
        prefix = "INSERT" if is_pg else f"INSERT {conflict}"
        suffix = conflict if is_pg else ""
        cursor.execute(
            f"{prefix} INTO stream_checkpoints "
            "(workspace_id, stream_id, shard_id, checkpoint_seq, fencing_token, "
            " state, state_format, last_sequence_no, virtual_clock_at, updated_at) "
            f"VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s) {suffix}",
            [
                _uuid_param(self._workspace_id),
                _uuid_param(self._stream_id),
                self._shard_id,
                checkpoint_seq,
                fencing_token,
                state,
                last_sequence_no,
                virtual_at,
                now,
            ],
        )
        return int(cursor.rowcount or 0)

    # -- load (§8.5 takeover) -----------------------------------------------------

    async def load(self) -> RestoredCheckpoint | None:
        """Load the persisted checkpoint row (``None`` → first start, §8.3)."""
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> RestoredCheckpoint | None:
        from generation.domain.models import StreamCheckpoint
        from generation.infra.compression import decompress

        with self._ws_scope():
            row = StreamCheckpoint.all_objects.filter(
                stream_id=self._stream_id, shard_id=self._shard_id
            ).first()
        if row is None:
            return None
        blob_json = decompress(bytes(row.state)).decode("utf-8")
        return RestoredCheckpoint(
            checkpoint_seq=int(row.checkpoint_seq),
            fencing_token=int(row.fencing_token),
            last_sequence_no=int(row.last_sequence_no),
            blob=json.loads(blob_json),
        )

    async def restore_into(self, shard: Shard, restored: RestoredCheckpoint) -> None:
        """Rehydrate ``shard`` from a loaded checkpoint (pool images + codec, §9.3)."""
        await asyncio.to_thread(self._restore_into_sync, shard, restored)

    def _restore_into_sync(self, shard: Shard, restored: RestoredCheckpoint) -> None:
        from dataforge_engine.behavior.pools import PooledEntity
        from generation.infra.snapshot_store import SnapshotSink

        snapshot = SnapshotSink(
            workspace_id=self._workspace_id,
            stream_id=self._stream_id,
            shard_id=self._shard_id,
        )
        # §9.3 step 2: load pool images into the engine before codec restore.
        # ``ensure_registered`` (called by ``restore_checkpoint`` too) is idempotent;
        # call it first so the per-type pools exist to receive the images.
        shard.ensure_registered()
        with self._ws_scope():
            for entity_type in shard.ir.entity_order:
                images = snapshot.load_pool_image(
                    entity_type=entity_type, snapshot_epoch=restored.checkpoint_seq
                )
                for image in images:
                    shard.pools.reindex_loaded(
                        _pooled_entity_from_image(PooledEntity, entity_type, image)
                    )
        # §9.3 steps 3-4: heap, cursors, arrival, sequence, traversals + counters.
        restore_checkpoint(shard, restored.blob)


def _pooled_entity_from_image(
    cls: type[Any], entity_type: str, image: Mapping[str, Any]
) -> Any:
    """Rebuild a ``PooledEntity`` from its ``snapshot_json`` image (§9.3 step 2).

    The inverse of :meth:`PooledEntity.snapshot_json` — the entity *type* is the
    pool the image belongs to (the caller iterates per type), so it is supplied
    separately. Indexes are rebuilt by ``reindex_loaded`` (derived state, §4.2).
    """
    return cls(
        entity_key=str(image["entity_key"]),
        entity_type=entity_type,
        attributes=dict(image["attributes"]),
        entity_version=int(image["entity_version"]),
        created_at=str(image["created_at"]),
        updated_at=str(image["updated_at"]),
        status=str(image.get("status", "live")),
        in_session=bool(image.get("in_session", False)),
    )


def _ensure_fencing_error_exported() -> type[FencingError]:
    """Keep :class:`FencingError` reachable from this module (re-export sanity)."""
    return FencingError
