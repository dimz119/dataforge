"""Seed derivation tree (ADR-0008; behavior-engine §7.1).

All randomness in DataForge derives from a single 63-bit stream ``seed`` through
exactly two HMAC-SHA256 levels plus a counter — no other source of entropy is
ever consulted (no ``os.urandom``, no unseeded ``random``). The tree is the
determinism unit: same ``seed`` ⇒ byte-identical canonical content (INV-GEN-3).

Derivation (behavior-engine §7.1, verbatim)::

    seed_bytes          = BE64(seed)                                 # 8 bytes, big-endian
    subseed(ns)         = HMAC-SHA256(key=seed_bytes, msg=utf8(ns))      # 32 bytes
    stream_key(ns, ctx) = HMAC-SHA256(key=subseed(ns), msg=utf8(ctx))   # 32 bytes
    draw(K, n)          = HMAC-SHA256(key=K, msg=BE64(n))            # 32-byte digest
    u64(K, n)           = bytes 0..8 of draw(K, n) as big-endian uint64
    u(K, n)             = u64(K, n) / 2**64                          # uniform [0, 1)

The four top-level namespaces are fixed forever: ``values``, ``transitions``,
``pools``, ``chaos``. The behavior engine draws from the first three; ``chaos`` is
derived (so it is reserved and namespace-isolated) but consumed only by the chaos
engine (Phase 9) — the behavior engine NEVER draws from it, which is why toggling
chaos can never perturb canonical content.

Pure Python: ``hashlib`` + ``hmac`` (stdlib) only (BE-ENG-1).

Stable import paths::

    from dataforge_engine.seeds import (
        Namespace, NAMESPACES,
        seed_bytes, subseed, stream_key,
        draw, u64, u, bits, two_u64,
        SeedTree,
    )
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final, Literal

# ---------------------------------------------------------------------------
# Namespaces (frozen by ADR-0008).
# ---------------------------------------------------------------------------

Namespace = Literal["values", "transitions", "pools", "chaos"]

NAMESPACES: Final[tuple[Namespace, ...]] = ("values", "transitions", "pools", "chaos")

# The stream seed domain (behavior-engine §7.1 / api R-3): [0, 2**63 - 1].
SEED_MIN: Final[int] = 0
SEED_MAX: Final[int] = (1 << 63) - 1

_U64_CEIL: Final[int] = 1 << 64
_U64_MASK: Final[int] = _U64_CEIL - 1


class SeedError(ValueError):
    """Raised when a seed or counter falls outside its declared domain."""


# ---------------------------------------------------------------------------
# Primitive derivation functions (pure; the §7.1 algebra).
# ---------------------------------------------------------------------------


def seed_bytes(seed: int) -> bytes:
    """``BE64(seed)`` — the 8-byte big-endian seed key for the first HMAC level."""
    if not SEED_MIN <= seed <= SEED_MAX:
        raise SeedError(f"seed {seed} outside [0, 2**63-1] (behavior-engine §7.1)")
    return seed.to_bytes(8, "big")


def subseed(seed: int, namespace: str) -> bytes:
    """``HMAC-SHA256(key=BE64(seed), msg=utf8(namespace))`` — 32-byte sub-seed."""
    return hmac.new(seed_bytes(seed), namespace.encode("utf-8"), hashlib.sha256).digest()


def stream_key(sub: bytes, ctx: str) -> bytes:
    """``HMAC-SHA256(key=subseed, msg=utf8(ctx))`` — 32-byte per-context PRF key.

    ``sub`` is a :func:`subseed` output (the namespace level); ``ctx`` is the
    draw-site context string from the §7.1 catalog (e.g. ``"session:{id}"``).
    """
    return hmac.new(sub, ctx.encode("utf-8"), hashlib.sha256).digest()


def draw(key: bytes, n: int) -> bytes:
    """``HMAC-SHA256(key=K, msg=BE64(n))`` — the 32-byte digest for counter ``n``.

    Counter-based (not stateful): ``draw(K, n)`` is a pure PRF evaluation, so the
    only mutable RNG state anywhere is the named uint64 counters callers keep —
    which is exactly what the checkpoint codec serializes (§9.1).
    """
    if n < 0:
        raise SeedError(f"draw counter must be >= 0, got {n}")
    return hmac.new(key, n.to_bytes(8, "big"), hashlib.sha256).digest()


def u64(key: bytes, n: int) -> int:
    """Bytes 0..8 of ``draw(K, n)`` as a big-endian uint64."""
    return int.from_bytes(draw(key, n)[:8], "big")


def u(key: bytes, n: int) -> float:
    """``u64(K, n) / 2**64`` — uniform in ``[0, 1)`` (the selection/dwell draw)."""
    return u64(key, n) / _U64_CEIL


def two_u64(key: bytes, n: int) -> tuple[int, int]:
    """Bytes 0..8 and 8..16 of one digest as two uint64s.

    Used by the background-mutation schedule (BE-B1): one digest decides both the
    occurrence draw (bytes 0..8) and the time-of-day draw (bytes 8..16).
    """
    digest = draw(key, n)
    return (
        int.from_bytes(digest[:8], "big"),
        int.from_bytes(digest[8:16], "big"),
    )


def bits(key: bytes, n: int, width: int) -> int:
    """The low ``width`` bits of the full 256-bit ``draw(K, n)`` digest.

    The UUIDv7 random-bit source (§7.2) needs 74 bits — more than one uint64 —
    so it reads the whole digest and masks. ``width`` must be ≤ 256.
    """
    if not 0 < width <= 256:
        raise SeedError(f"bits width must be in (0, 256], got {width}")
    full = int.from_bytes(draw(key, n), "big")
    return full & ((1 << width) - 1)


# ---------------------------------------------------------------------------
# SeedTree — the convenience root the engine carries.
# ---------------------------------------------------------------------------


class SeedTree:
    """The per-stream seed root: caches the four sub-seeds, mints ``stream_key``s.

    One instance per stream/shard generation. Holds no draw state — counters live
    in the per-traversal/per-context cursors the interpreter and pools keep — so a
    :class:`SeedTree` is itself stateless and freely shareable.
    """

    __slots__ = ("_seed", "_subseeds")

    def __init__(self, seed: int) -> None:
        # Validate + precompute the four namespace sub-seeds once.
        self._seed = seed
        self._subseeds: dict[str, bytes] = {ns: subseed(seed, ns) for ns in NAMESPACES}

    @property
    def seed(self) -> int:
        return self._seed

    def subseed(self, namespace: Namespace) -> bytes:
        """The cached 32-byte sub-seed for one namespace."""
        return self._subseeds[namespace]

    def key(self, namespace: Namespace, ctx: str) -> bytes:
        """``stream_key(subseed(namespace), ctx)`` — the PRF key for a draw site."""
        return stream_key(self._subseeds[namespace], ctx)
