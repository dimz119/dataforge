"""The chaos sub-seed and the PRF draw (chaos-engine §4.1, P9-02).

All chaos randomness derives from the stream's ``chaos`` sub-seed —
``chaos_subseed = HMAC-SHA256(stream_seed, "chaos")`` (the ``chaos`` namespace of
:mod:`dataforge_engine.seeds`, ADR-0008). The behavior engine NEVER draws from
this namespace (INV-CHA-1), so toggling chaos can never perturb canonical content.

Every chaos decision is a pseudo-random function evaluation keyed on the event's
identity — never a stateful RNG cursor (§4.1)::

    draw(mode, event_id, label[, instance])
      = first_8_bytes(
          HMAC-SHA256(chaos_subseed, mode ‖ ":" ‖ event_id ‖ ":" ‖ label
                                     [‖ ":" ‖ instance]) ) as uint64 / 2**64
      → u ∈ [0, 1)

Because the draws are keyed on identifiers that are themselves deterministic
(``event_id`` is reproducible from the seed), the chain is closed: same
``(manifest_version, seed, config)`` ⇒ identical chaos decisions (INV-CHA-2).
Independence (§4.2) follows from PRF keying, not draw-order consumption: a
decision depends only on the event's identity, never on tick/batch boundaries.

Pure Python: ``hashlib`` + ``hmac`` (stdlib) only (BE-ENG-1; engine purity
import-linter contract). No wall-clock, no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from dataforge_engine.seeds import subseed

# The fixed chaos namespace string (ADR-0008; seeds.NAMESPACES).
CHAOS_NAMESPACE: Final[str] = "chaos"

_U64_CEIL: Final[int] = 1 << 64


def chaos_subseed(stream_seed: int) -> bytes:
    """``HMAC-SHA256(BE64(stream_seed), "chaos")`` — the 32-byte chaos sub-seed.

    Thin alias over :func:`dataforge_engine.seeds.subseed` pinned to the ``chaos``
    namespace so the chaos engine never reaches into the behavior namespaces.
    """
    return subseed(stream_seed, CHAOS_NAMESPACE)


def _message(mode: str, event_id: str, label: str, instance: int | None) -> bytes:
    """Assemble the PRF message ``mode:event_id:label[:instance]`` (§4.1)."""
    parts = [mode, event_id, label] if instance is None else [mode, event_id, label, str(instance)]
    return ":".join(parts).encode("utf-8")


def digest(
    subseed_bytes: bytes,
    mode: str,
    event_id: str,
    label: str,
    instance: int | None = None,
) -> bytes:
    """The full 32-byte ``HMAC-SHA256`` digest behind a draw (before truncation).

    The deterministic ``injection_id`` assembly (record §7.1) consumes the full
    digest's bytes for its 74 random bits, so it is exposed here alongside
    :func:`draw_u`.
    """
    message = _message(mode, event_id, label, instance)
    return hmac.new(subseed_bytes, message, hashlib.sha256).digest()


def draw_u(
    subseed_bytes: bytes,
    mode: str,
    event_id: str,
    label: str,
    instance: int | None = None,
) -> float:
    """``u ∈ [0, 1)`` — the first 8 bytes of the digest as a uint64 / 2**64 (§4.1)."""
    return draw_u64(subseed_bytes, mode, event_id, label, instance) / _U64_CEIL


def draw_u64(
    subseed_bytes: bytes,
    mode: str,
    event_id: str,
    label: str,
    instance: int | None = None,
) -> int:
    """The first 8 bytes of the digest as a big-endian uint64 (the raw draw)."""
    return int.from_bytes(digest(subseed_bytes, mode, event_id, label, instance)[:8], "big")


def weighted_choice(u: float, weights: list[float]) -> int:
    """Index into ``weights`` for a draw ``u ∈ [0, 1)`` (deterministic, PRF-keyed).

    Used by ``duplicates`` (copy count) and the corruption-kind picks: ``u`` scales
    across the cumulative weight mass so the same ``(u, weights)`` always selects
    the same index. ``weights`` must be non-empty with a positive total.
    """
    total = sum(weights)
    if total <= 0:
        raise ValueError("weighted_choice requires a positive total weight")
    target = u * total
    cumulative = 0.0
    for index, weight in enumerate(weights):
        cumulative += weight
        if target < cumulative:
            return index
    return len(weights) - 1  # u→1.0 edge: the last bucket
