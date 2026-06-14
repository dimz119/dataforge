"""``event_buffer`` write store — transactional batch INSERT + ``buffer_seq``
assignment (database-schema §6.1; delivery-channels §4.2; ADR-0013).

Note on COPY vs INSERT: delivery-channels §4.2 names a "transactional COPY" for
throughput, but ``event_buffer`` is a FORCE-RLS Class T table and Postgres rejects
``COPY FROM`` on RLS-enabled tables for the NOBYPASSRLS runtime role. The store
therefore writes one multi-row ``INSERT`` per batch (≤ 500 rows, BW-2), which
enforces the WITH CHECK policy per row, is a single statement/transaction, and
preserves every BW guarantee (order, monotonic ``buffer_seq``, offset-after-commit).

The buffer-writer sink (``runner.sinks.buffer_writer``) hands this store an ordered
batch of **delivered-shape** envelopes (already ``strip_internal``-ed, SB-2) for one
stream; the store:

1. assigns each row a strictly-increasing per-stream ``buffer_seq`` (BW-6), starting
   from the recovered high-water mark (BW-8);
2. assigns ``partition_ts = now()`` clamped non-decreasing per stream so
   ``(partition_ts, buffer_seq)`` order is identical to ``buffer_seq`` order (BW-6);
3. writes the whole batch in **one transaction** via ``COPY`` (Postgres, preferred,
   BW-2) or a multi-row ``INSERT`` (SQLite unit lane), in batch order.

Offsets are committed by the host **only after** this transaction commits
(at-least-once INV-DEL-3, BW-3): a crash between DB commit and offset commit
redelivers the tail batch under fresh ``buffer_seq`` values — the rare server-side
duplicate the channel's at-least-once contract already licenses. The writer never
deduplicates on ``event_id`` (BW-4).

Single-writer-per-stream (BW-7) makes ``buffer_seq`` monotonic without coordination:
one shard → one internal partition → one consumer in MVP. This store holds the
per-stream counter in memory and recovers it from the DB on first touch / restart
(``SELECT max(buffer_seq) … WHERE stream_id = $1``, always within retention).

Writes go through the Django ``default`` connection (the ``dataforge_app``
NOBYPASSRLS role at runtime) so RLS bites: the row's ``workspace_id`` must equal the
armed ``app.workspace_id`` GUC. The caller arms the workspace context before
delivering a batch (per-batch ``workspace_id``, SINK-7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from django.db import connection, transaction

from dataforge_engine.envelope import canonical_serialize_str

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dataforge_engine.envelope import DeliveredEnvelope

__all__ = ["BufferStore", "BufferWriteResult"]

_TABLE = "event_buffer"
_COLUMNS = (
    "workspace_id",
    "stream_id",
    "partition_ts",
    "buffer_seq",
    "event_id",
    "event_type",
    "occurred_at",
    "emitted_at",
    "envelope",
)


class BufferWriteResult:
    """The outcome of one :meth:`BufferStore.write_batch` (delivery-channels §4.2)."""

    __slots__ = ("first_buffer_seq", "last_buffer_seq", "rows_written")

    def __init__(self, *, rows_written: int, first_buffer_seq: int, last_buffer_seq: int) -> None:
        self.rows_written = rows_written
        self.first_buffer_seq = first_buffer_seq
        self.last_buffer_seq = last_buffer_seq


def _uuid_param(value: str) -> str:
    """Format a UUID for the active DB's raw-SQL binding (dashed PG / 32-hex SQLite)."""
    parsed = uuid.UUID(value)
    return str(parsed) if connection.vendor == "postgresql" else parsed.hex


def _parse_ts(value: str) -> datetime:
    """RFC-3339 envelope timestamp string → tz-aware UTC ``datetime`` (event-model)."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class BufferStore:
    """Per-stream monotonic writer into ``event_buffer`` (single-writer, BW-6/7).

    One instance per stream the buffer-writer owns; the counter + the clamped
    ``partition_ts`` are kept in memory and recovered from the DB on first use.
    """

    def __init__(
        self, *, workspace_id: str, stream_id: str, clock: Any | None = None
    ) -> None:
        self._workspace_id = workspace_id
        self._stream_id = stream_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._next_seq: int | None = None  # recovered lazily (BW-8)
        self._last_partition_ts: datetime | None = None
        self.rows_written = 0

    # -- counter recovery (BW-8) -------------------------------------------------

    def _recover_counter(self) -> int:
        """``SELECT max(buffer_seq) … WHERE stream_id = $1`` → next free seq (BW-8).

        Always within the 48 h retention window, so this is the true high-water
        mark; the writer continues from ``max + 1`` (or 1 on an empty stream).
        """
        sid = _uuid_param(self._stream_id)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT max(buffer_seq) FROM {_TABLE} WHERE stream_id = %s", [sid]
            )
            row = cursor.fetchone()
        current = row[0] if row and row[0] is not None else 0
        return int(current) + 1

    def _ensure_recovered(self) -> None:
        if self._next_seq is None:
            self._next_seq = self._recover_counter()

    def _clamped_partition_ts(self) -> datetime:
        """``now()`` clamped non-decreasing per stream (BW-6) — so ``(partition_ts,
        buffer_seq)`` order equals ``buffer_seq`` order even if the wall clock skews
        backward.
        """
        now = self._clock().astimezone(UTC)
        if self._last_partition_ts is not None and now < self._last_partition_ts:
            now = self._last_partition_ts
        self._last_partition_ts = now
        return now

    # -- write (BW-2 transactional COPY) -----------------------------------------

    def write_batch(self, envelopes: Sequence[DeliveredEnvelope]) -> BufferWriteResult:
        """Assign ``buffer_seq``/``partition_ts`` and write the batch in one txn.

        ``envelopes`` are already delivered-shape (20 keys, ``_df`` stripped) in
        offset order. Returns the assigned ``buffer_seq`` span. An empty batch is a
        no-op. Single transaction so the host's offset commit (BW-3) only follows a
        durable insert; on failure the whole batch rolls back and the in-memory
        counter is *not* advanced (re-derived from the DB on the next attempt).
        """
        if not envelopes:
            return BufferWriteResult(rows_written=0, first_buffer_seq=-1, last_buffer_seq=-1)
        self._ensure_recovered()
        assert self._next_seq is not None

        start_seq = self._next_seq
        ws_id = _uuid_param(self._workspace_id)
        sid = _uuid_param(self._stream_id)
        rows: list[tuple[Any, ...]] = []
        seq = start_seq
        for env in envelopes:
            partition_ts = self._clamped_partition_ts()
            rows.append(
                (
                    ws_id,
                    sid,
                    partition_ts,
                    seq,
                    _uuid_param(str(env["event_id"])),
                    str(env["event_type"]),
                    _parse_ts(str(env["occurred_at"])),
                    _parse_ts(str(env["emitted_at"])),
                    # S-2 canonical serialization of the *delivered* shape (BW-5).
                    canonical_serialize_str(env),
                )
            )
            seq += 1
        last_seq = seq - 1

        with transaction.atomic():
            # A multi-row INSERT (not COPY) is mandatory on Postgres: ``event_buffer``
            # is a FORCE-RLS Class T table, and Postgres rejects ``COPY FROM`` on
            # RLS-enabled tables for the NOBYPASSRLS runtime role ("COPY FROM not
            # supported with row-level security; use INSERT statements instead").
            # INSERT enforces the WITH CHECK policy per row (the workspace is armed by
            # the caller, SINK-7) and stays one transaction in batch order (BW-2/BW-3).
            self._insert_rows(rows)

        # Advance the in-memory counter only after a durable commit.
        self._next_seq = seq
        self.rows_written += len(rows)
        return BufferWriteResult(
            rows_written=len(rows), first_buffer_seq=start_seq, last_buffer_seq=last_seq
        )

    def _insert_rows(self, rows: list[tuple[Any, ...]]) -> None:
        """One multi-row INSERT (batch order, single statement) on both backends.

        ``envelope`` is stored as the canonical-JSON STRING verbatim (the column is
        ``text`` on Postgres, not ``jsonb``) so the delivered bytes survive byte-for-
        byte for the S-3 cross-channel identity contract (BW-5). One statement keeps
        the whole batch atomic in ``write_batch``'s transaction so the host commits
        Kafka offsets only after a durable insert.
        """
        placeholder = "(" + ", ".join(["%s"] * len(_COLUMNS)) + ")"
        values_sql = ", ".join([placeholder] * len(rows))
        cols = ", ".join(_COLUMNS)
        params: list[Any] = [v for row in rows for v in row]
        sql = f"INSERT INTO {_TABLE} ({cols}) VALUES {values_sql}"
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
