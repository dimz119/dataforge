"""Deterministic UUIDv7 builder for ``event_id`` (event-model §2.2.1).

A naive UUIDv7 embeds wall-clock milliseconds and 74 bits of OS randomness, both
of which would break INV-GEN-3 (byte-identical canonical sequences regardless of
wall pacing or machine). Envelope ``1.0`` therefore pins:

* **Timestamp bits (48)** = milliseconds of ``occurred_at`` (*simulated* time).
* **Random bits (74)** = drawn from the stream's seeded PRNG (``values``
  sub-seed namespace, ADR-0008), one draw per event.

RFC 9562 UUIDv7 bit layout (128 bits, big-endian):

    ┌──────────────── 48 ────────────────┬─ 4 ─┬── 12 ──┬─ 2 ─┬─────── 62 ───────┐
    │            unix_ts_ms               │ ver │ rand_a │ var │      rand_b       │
    └─────────────────────────────────────┴─────┴────────┴─────┴───────────────────┘

``ver`` = ``0b0111`` (7); ``var`` = ``0b10``. The 74 random bits are ``rand_a``
(12) ‖ ``rand_b`` (62). The caller owns the seed stream and supplies the 74 bits
through :class:`RandomBitsSource` — this module NEVER calls ``os.urandom`` or the
stdlib ``random`` module, so determinism is the caller's contract to honour and
this builder is a pure function of ``(occurred_at_ms, random_74)``.

Pure Python; ``uuid``/``datetime`` only (BE-ENG-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from .timestamps import occurred_at_ms

if TYPE_CHECKING:
    from datetime import datetime

# Bit-width constants of the RFC 9562 v7 layout.
_TS_BITS = 48
_RAND_A_BITS = 12
_RAND_B_BITS = 62
_RANDOM_BITS = _RAND_A_BITS + _RAND_B_BITS  # 74, per event-model §2.2.1

_VERSION = 0x7
_VARIANT = 0b10

_MAX_TS = (1 << _TS_BITS) - 1
_MAX_RANDOM = (1 << _RANDOM_BITS) - 1
_RAND_B_MASK = (1 << _RAND_B_BITS) - 1


class EventIdError(ValueError):
    """Raised when timestamp or random bits fall outside the v7 layout bounds."""


class RandomBitsSource(Protocol):
    """The seed-stream contract the caller implements (ADR-0008 ``values``
    namespace). One call per event must return the 74 random bits as an
    unsigned integer in ``[0, 2**74)``. The caller owns reproducibility: the
    same seed + draw order must return the same value every run.
    """

    def next_random_74(self) -> int:
        """Return the next 74-bit unsigned random integer from the seed stream."""
        ...


def build_uuidv7(*, timestamp_ms: int, random_74: int) -> UUID:
    """Assemble a UUIDv7 from explicit 48-bit ms and 74 random bits (pure).

    This is the primitive: callers that already hold the simulated ms and a draw
    use it directly; :func:`event_id_for` is the convenience wrapper that maps a
    canonical ``occurred_at`` instant and a :class:`RandomBitsSource`.
    """
    if not 0 <= timestamp_ms <= _MAX_TS:
        raise EventIdError(f"timestamp_ms {timestamp_ms} does not fit 48 bits")
    if not 0 <= random_74 <= _MAX_RANDOM:
        raise EventIdError(f"random_74 {random_74} does not fit 74 bits")

    rand_a = (random_74 >> _RAND_B_BITS) & ((1 << _RAND_A_BITS) - 1)
    rand_b = random_74 & _RAND_B_MASK

    value = (
        (timestamp_ms & _MAX_TS) << 80
        | (_VERSION << 76)
        | (rand_a << 64)
        | (_VARIANT << 62)
        | rand_b
    )
    return UUID(int=value)


def event_id_for(occurred_at: datetime, source: RandomBitsSource) -> str:
    """Deterministic ``event_id`` (lowercase canonical UUIDv7 string) for an event.

    Timestamp bits come from ``occurred_at`` (simulated) milliseconds; the 74
    random bits come from one draw on the caller-owned seed stream. The returned
    string is the lowercase RFC 9562 form used on the wire (event-model §2.1
    field 2).
    """
    return str(
        build_uuidv7(
            timestamp_ms=occurred_at_ms(occurred_at),
            random_74=source.next_random_74(),
        )
    )
