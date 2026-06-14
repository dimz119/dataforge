"""Engine ports — the host-agnostic interfaces ``dataforge_engine.behavior``
depends on (backend-architecture §4.2; behavior-engine §1, §10).

The same engine code runs in three hosts — the runner shard worker (Phase 5), the
Layer-3 dry-run Celery worker, and pytest golden replay — so every side effect and
the wall clock are *injected* through these :class:`typing.Protocol` interfaces.
The engine never imports Django, redis, psycopg, or confluent_kafka (BE-ENG-1;
import-linter contract 2); a host supplies concrete adapters.

Ports defined here:

* :class:`WallClock` — the injected ``now`` (BE-ENG-2): the ONLY wall-clock read
  the engine performs, used solely for ``emitted_at`` stamping and token-bucket
  refill. Golden tests inject a deterministic clock.
* :class:`LedgerSink` — durable append of canonical internal envelopes
  (INV-GEN-5); the engine emits, never reads back.
* :class:`PoolStore` — the Tier-2 write-behind mirror + snapshot hooks
  (behavior-engine §4.1/§4.3). Tier-1 (the authoritative working set) is owned by
  the engine in-process; this port is the optional Redis hot-state seam.
* :class:`RandomBitsSource` — re-exported from the envelope library: the 74-bit
  source the UUIDv7 builder consumes (§7.2). The engine binds it to the seed tree.

These are structural Protocols: a host class satisfies a port by shape, with no
inheritance. Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

# Re-export the envelope's RandomBitsSource so engine + hosts share one symbol.
from dataforge_engine.envelope import RandomBitsSource
from dataforge_engine.envelope.types import JSONValue

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope

__all__ = [
    "LedgerSink",
    "PoolRecordImage",
    "PoolStore",
    "RandomBitsSource",
    "SnapshotSink",
    "WallClock",
]

# A serialized pooled-entity record image (the JSON the Redis hash and snapshot
# JSONL carry; behavior-engine §4.2). Keys are entity attribute names plus the
# runtime-maintained metadata; values are JSON scalars/containers.
type PoolRecordImage = Mapping[str, JSONValue]


@runtime_checkable
class WallClock(Protocol):
    """The injected wall clock (BE-ENG-2). Returns tz-aware UTC ``datetime``.

    This is the single wall-clock dependency of the engine. In production a host
    wraps ``datetime.now(UTC)``; the golden harness injects a deterministic clock
    (e.g. 1 ms per event) so ``emitted_at`` is pinned and byte-identity holds
    across the full envelope (testing-strategy §6).
    """

    def now(self) -> datetime:
        """The current wall-clock instant as a tz-aware UTC ``datetime``."""
        ...


@runtime_checkable
class LedgerSink(Protocol):
    """Durable, idempotent append of canonical internal envelopes (INV-GEN-5).

    The pipeline appends each pass's batch here *before* handing it to chaos
    (behavior-engine §10). Append is idempotent on ``(stream_id, shard_id,
    sequence_no)`` (the host implements ``ON CONFLICT DO NOTHING``), so a crash +
    deterministic re-generation re-appends byte-identical rows harmlessly.

    The engine never reads the ledger back; its memory of the past is the
    checkpoint + pools.
    """

    def append(self, envelopes: Sequence[InternalEnvelope]) -> None:
        """Durably append a batch of canonical envelopes in order."""
        ...


@runtime_checkable
class SnapshotSink(Protocol):
    """Tier-3 durable pool-image hook (behavior-engine §4.1, §9.1).

    One full image per (stream, shard, entity_type) per checkpoint cycle, stamped
    with the upcoming ``checkpoint_seq`` (the commit-marker rule, database-schema
    §5.4). The codec calls this; the host persists to ``entity_pool_snapshots``.
    """

    def write_pool_image(
        self,
        *,
        entity_type: str,
        snapshot_epoch: int,
        records: Iterable[Mapping[str, JSONValue]],
    ) -> None:
        """Persist a full pool image for one entity type at one checkpoint epoch."""
        ...

    def load_pool_image(
        self, *, entity_type: str, snapshot_epoch: int
    ) -> Iterable[Mapping[str, JSONValue]]:
        """Load the persisted image for restore (behavior-engine §9.3 step 2)."""
        ...


@runtime_checkable
class PoolStore(Protocol):
    """Tier-2 Redis hot-state write-behind mirror (behavior-engine §4.1/§4.3).

    Optional: the in-process Tier-1 working set is authoritative and sufficient
    for single-host batch generation (the L3 dry run and golden replay run with a
    no-op store). A production host supplies a Redis-backed adapter so the §4.3
    key shapes (``pool:{stream}:{shard}:{type}``) are populated for introspection
    and the Phase-11 cross-shard read path. Never the interpreter's read path for
    owned entities.

    All methods are write-behind (flushed once per tick); none is on the hot read
    path, so the engine tolerates a slow or absent store without correctness loss.
    """

    def flush_records(
        self,
        *,
        entity_type: str,
        upserts: Mapping[str, Mapping[str, JSONValue]],
        deletes: Iterable[str],
    ) -> None:
        """Pipeline the tick's record upserts/deletes for one entity type."""
        ...

    def overwrite(
        self,
        *,
        entity_type: str,
        records: Mapping[str, Mapping[str, JSONValue]],
    ) -> None:
        """Wholesale-overwrite a type's hash (restore rebuilds Tier-2, §9.3 step 2)."""
        ...
