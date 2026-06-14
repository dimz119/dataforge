"""Runtime RNG layer over the seed tree (behavior-engine §7.1, §7.2).

The :mod:`dataforge_engine.seeds` module is the stateless PRF algebra; this module
adds the *stateful* runtime layer the interpreter and pools use:

* :class:`Cursor` — a named counter bound to one ``(namespace, ctx)`` PRF key. It
  is the only mutable RNG state in the engine; its ``position`` is exactly the
  "RNG cursor" the checkpoint serializes (§9.1). Each draw advances by one (or by
  the fixed per-call count of a multi-draw generator, §7.3), so adding an
  attribute to a *new* manifest version never scrambles existing values.
* :class:`TraversalRng` — the two cursors a session/lifecycle traversal owns: the
  ``transitions`` cursor (selection + dwell draws) and the ``values`` cursor
  (payload-attribute draws + ``event_id`` digests). Per-traversal isolation
  (§7.1) means one traversal's draw count never shifts another's.
* :class:`UuidBits` — a :class:`~dataforge_engine.ports.RandomBitsSource` adapter
  that feeds the envelope's deterministic UUIDv7 builder 74 bits per call from a
  ``values``/``transitions`` cursor (§7.2).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataforge_engine.seeds import SeedTree, draw, two_u64, u, u64

_U64_CEIL = 1 << 64
_UUID_RANDOM_BITS = 74


class Cursor:
    """A counter-based draw stream bound to one PRF key.

    ``position`` is the next counter to consume. ``u()`` / ``u64()`` / ``bytes32()``
    each draw at the current position then advance by one. Restoring a cursor is
    just setting ``position`` — no log replay (§9.1 "RNG cursor positions").
    """

    __slots__ = ("_key", "position")

    def __init__(self, key: bytes, position: int = 0) -> None:
        self._key = key
        self.position = position

    def u(self) -> float:
        """One uniform ``[0, 1)`` draw, advancing the cursor."""
        value = u(self._key, self.position)
        self.position += 1
        return value

    def u64(self) -> int:
        """One uint64 draw, advancing the cursor."""
        value = u64(self._key, self.position)
        self.position += 1
        return value

    def bytes32(self) -> bytes:
        """The full 32-byte digest at the current position, advancing the cursor."""
        value = draw(self._key, self.position)
        self.position += 1
        return value

    def two_u64(self) -> tuple[int, int]:
        """Two uint64s from one digest (BE-B1 occurrence + time-of-day), advancing."""
        value = two_u64(self._key, self.position)
        self.position += 1
        return value

    def bits_74(self) -> int:
        """74 random bits from one digest (the UUIDv7 source, §7.2), advancing."""
        full = int.from_bytes(draw(self._key, self.position), "big")
        self.position += 1
        return full & ((1 << _UUID_RANDOM_BITS) - 1)


class UuidBits:
    """:class:`~dataforge_engine.ports.RandomBitsSource` over a :class:`Cursor`.

    The envelope's :func:`event_id_for` calls ``next_random_74`` once per id; this
    adapter draws those 74 bits from the traversal's ``values`` cursor (in-session
    + lifecycle events) or, for ``session_id`` minting, the ``values`` cursor keyed
    on the arrival (§7.1 ``values:arrival:{shard}:{n}``).
    """

    __slots__ = ("_cursor",)

    def __init__(self, cursor: Cursor) -> None:
        self._cursor = cursor

    def next_random_74(self) -> int:
        return self._cursor.bits_74()


class TraversalRng:
    """The two cursors a single traversal owns (§7.1).

    A session keys both cursors on ``session:{session_id}``; a lifecycle keys on
    ``lifecycle:{machine}:{subject_key}``. The interpreter draws selection/dwell
    from :attr:`transitions` and payload/event_id from :attr:`values`.
    """

    __slots__ = ("transitions", "uuid_bits", "values")

    def __init__(self, transitions: Cursor, values: Cursor) -> None:
        self.transitions = transitions
        self.values = values
        # event_id draws ride the values cursor (§7.1 values:session/lifecycle).
        self.uuid_bits = UuidBits(values)


def traversal_rng(
    tree: SeedTree, *, transitions_ctx: str, values_ctx: str,
    transitions_pos: int = 0, values_pos: int = 0,
) -> TraversalRng:
    """Build a :class:`TraversalRng` for one traversal from the seed tree.

    ``*_pos`` resume the cursors from a checkpoint (§9.3); both default to 0 for a
    fresh traversal.
    """
    return TraversalRng(
        Cursor(tree.key("transitions", transitions_ctx), transitions_pos),
        Cursor(tree.key("values", values_ctx), values_pos),
    )
