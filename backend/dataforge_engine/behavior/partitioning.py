"""Actor-to-shard partitioning (behavior-engine §3.5; scaling-strategy §3 sharding).

Phase 11 multi-shard generation. A stream with ``shard_count = N`` runs ``N`` shard
workers, each an independent :class:`~dataforge_engine.behavior.shard.Shard` with its
own gapless ``sequence_no`` (INV-GEN-7). For the shards to generate a **disjoint**
actor population — no two shards ever drive sessions for the same actor, so there is
no cross-shard duplication of an actor's lifecycle — each actor is assigned to exactly
one owning shard by a **stable, deterministic** function of its primary key (the
"PK-1" partition entity key, event-model §2.2.3)::

    owning_shard(actor_key) = blake2b(actor_key) mod shard_count

The hash is content-addressed (``hashlib.blake2b``), so the assignment is identical
across every process, restart, and host — unlike Python's builtin ``hash()`` of a
string, which is salted per-process (``PYTHONHASHSEED``) and would scatter the same
actor to different shards on restart, breaking GOLD-D byte-identical continuation.

Every shard still **seeds the full catalog** (each shard needs the complete entity
pools for relationship/FK resolution and seed snapshots), but a shard only **binds
sessions** for the actors it owns (:meth:`Shard._bind_actor` filters by this
predicate). With ``shard_count = 1`` the predicate is always true, so the single-shard
MVP path is byte-for-byte unchanged (``0 == anything mod 1``).

Pure Python: ``hashlib`` (stdlib) only (BE-ENG-1).
"""

from __future__ import annotations

import hashlib

# A short digest is plenty for a uniform mod-N bucketing and keeps the hash cheap on
# the binding hot path; 8 bytes → a 64-bit unsigned int, uniform over any N ≤ 64.
_DIGEST_BYTES = 8


def shard_for_key(actor_key: str, shard_count: int) -> int:
    """The shard id (``0 … shard_count-1``) that owns ``actor_key``.

    Stable across processes/hosts/restarts (content-addressed ``blake2b``), so an
    actor's owning shard never changes — the property GOLD-D continuation and the
    "disjoint actor subset per shard" invariant both rest on. ``shard_count <= 1``
    short-circuits to ``0`` (the single-shard MVP) without hashing.
    """
    if shard_count <= 1:
        return 0
    digest = hashlib.blake2b(actor_key.encode("utf-8"), digest_size=_DIGEST_BYTES).digest()
    return int.from_bytes(digest, "big") % shard_count


def owns_key(actor_key: str, *, shard_id: int, shard_count: int) -> bool:
    """Does the shard ``shard_id`` own ``actor_key`` under an ``shard_count``-way split?

    The per-shard eligibility predicate :meth:`Shard._bind_actor` applies so each
    shard drives a disjoint actor population (no cross-shard session duplication).
    """
    return shard_for_key(actor_key, shard_count) == shard_id
