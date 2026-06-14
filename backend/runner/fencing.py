"""Fencing-token enforcement primitives (backend-architecture §8.2; INV-STR-2).

A *fencing token* is a strictly-monotonic-per-shard integer issued on every lease
acquisition (``INCR df:fence:{stream}:{shard}``). It is the mechanism that stops a
*zombie* — a runner that lost its lease (TTL expiry / missed heartbeat) but has not
yet noticed — from corrupting durable state after a new holder has taken over.

INV-STR-2 (domain-model §2.5): *at most one live lease per (stream, shard), and a
runner that lost its lease must stop emitting before the new holder's first tick.*
The lease (a Redis TTL) guarantees the "at most one live" half; the fencing token
guarantees the "stops emitting" half at every durable surface, because the new
holder always carries a **higher** token than any zombie.

This module is the shared kernel the §8.2 enforcement points use:

* **Checkpoints** — ``CheckpointStore.save`` is a conditional write
  ``… WHERE fencing_token <= %(mine)s``; a stale token's write affects zero rows
  and must raise :class:`FencingError` (state can never roll back).
* **Ground-truth ledger** — append is idempotent on ``(stream, shard, sequence_no)``
  with ``ON CONFLICT DO NOTHING`` (determinism makes old/new holders' rows
  identical), so it does not need to *raise* — it absorbs the zombie silently.
* **Injection records** — same idempotent-insert pattern keyed by deterministic id.
* **Kafka publish** — cannot be transactionally fenced; bounded to ≤ 1 in-flight
  tick after lease loss (the supervisor cancels the worker between pipeline steps).

The conditional-write *enforcement* surfaces (checkpoint, injection) call
:func:`enforce_conditional_write` after issuing their guarded ``UPDATE``/``INSERT``,
passing the affected-row count: zero rows under a guard means a strictly-greater
token already won, i.e. a takeover happened — the caller is fenced.

The runner host owns no ORM imports beyond the seams it is handed, so this module
is framework-free (it never touches Postgres directly): it expresses the *policy*
(``fencing_token <= mine`` ⇒ keep; rows-affected == 0 ⇒ fenced) and the callers
supply the SQL. That keeps the rule in one place and unit-testable without a DB.
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "FENCE_KEY",
    "FencingError",
    "enforce_conditional_write",
    "fence_key",
    "is_fresh_token",
]

# §8.2 fence counter key template. INCR on this key issues the next token; it is
# never reset, so tokens are strictly monotonic per shard across all time (and
# across runner crashes — the counter lives in Redis, not in any process).
FENCE_KEY = "df:fence:{stream_id}:{shard_id}"


def fence_key(stream_id: UUID | str, shard_id: int) -> str:
    """The §8.2 Redis fence-counter key for a (stream, shard)."""
    return FENCE_KEY.format(stream_id=stream_id, shard_id=shard_id)


class FencingError(RuntimeError):
    """A durable write was rejected because the writer's fencing token is stale.

    Raised at the conditional-write enforcement points (§8.2): the row already
    carries a fencing token strictly greater than the writer's, meaning a newer
    lease holder has taken over this (stream, shard). The losing writer is a
    zombie and must stop emitting immediately (INV-STR-2). The supervisor catches
    this to tear down the orphaned shard worker; the durable surface is untouched,
    so state can never roll back.
    """

    def __init__(
        self,
        stream_id: UUID | str,
        shard_id: int,
        my_token: int,
        surface: str = "checkpoint",
    ) -> None:
        self.stream_id = stream_id
        self.shard_id = shard_id
        self.my_token = my_token
        self.surface = surface
        super().__init__(
            f"fenced on {surface} for stream={stream_id} shard={shard_id}: "
            f"token {my_token} is stale (a newer lease holder has taken over)"
        )


def is_fresh_token(my_token: int, stored_token: int | None) -> bool:
    """Policy: is ``my_token`` allowed to write over ``stored_token``?

    The §8.2 guard is ``WHERE fencing_token <= %(mine)s`` — a writer may overwrite a
    row whose stored token is **less than or equal to** its own. ``None`` (no row
    yet) is always writable. This is the in-Python mirror of that SQL predicate, for
    callers that read-then-write (most use the atomic guarded ``UPDATE`` and check
    rows-affected via :func:`enforce_conditional_write` instead).
    """
    return stored_token is None or stored_token <= my_token


def enforce_conditional_write(
    rows_affected: int,
    *,
    stream_id: UUID | str,
    shard_id: int,
    my_token: int,
    surface: str = "checkpoint",
) -> None:
    """Raise :class:`FencingError` if a guarded conditional write changed no rows.

    The §8.2 enforcement pattern is an atomic guarded statement, e.g.::

        UPDATE stream_checkpoints
           SET blob = %(blob)s, fencing_token = %(mine)s, ...
         WHERE stream_id = %(stream)s AND shard_id = %(shard)s
           AND fencing_token <= %(mine)s

    If the stored token is strictly greater than ``my_token`` the ``WHERE`` matches
    nothing and ``rows_affected`` is ``0`` — a newer holder has taken over, so the
    writer is fenced. Any positive count means the write landed (the guard held).

    Callers pass the cursor's ``rowcount`` (or ORM ``QuerySet.update()`` return).
    This keeps the fencing rule in one place while the SQL stays in the infra layer.
    """
    if rows_affected == 0:
        raise FencingError(stream_id, shard_id, my_token, surface=surface)
