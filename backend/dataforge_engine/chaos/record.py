"""The ``InjectionRecord`` — the ground-truth answer-key row (chaos-engine §7.1).

One record per injection, append-only (the ``chaos_injections`` table). The
record is written BEFORE the affected instance is published, buffered, or
suppressed (INV-CHA-4) — that ordering is why answer-key counts match delivered
chaos *to the event*. This module carries the framework-free record shape plus
the deterministic ``injection_id`` assembly; the Django ``chaos`` app persists it.

``injection_id`` is a deterministic UUIDv7 (§7.1):

* timestamp bits (48) = the canonical event's ``occurred_at`` simulated ms;
* rand_a/rand_b bits (74) = bytes of the FULL 32-byte HMAC digest behind
  ``draw(mode, event_id, "injection_id"[, instance])`` (§4.1's digest before its
  8-byte truncation).

No wall-clock bit anywhere ⇒ idempotent re-recording under tick retries (CR-7)
and byte-stable across golden replays (§11).

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict
from uuid import UUID

from dataforge_engine.envelope.event_id import build_uuidv7

from .policy import ChaosMode
from .prf import digest

# The fixed PRF label for the injection_id draw (§4.1 draw catalog augmentation —
# documented here as required before use).
INJECTION_ID_LABEL = "injection_id"

_RANDOM_74_MASK = (1 << 74) - 1


def _occurred_at_ms(occurred_at: str) -> int:
    """Milliseconds of the canonical ``occurred_at`` RFC-3339 string (§7.1 ts bits).

    The envelope carries ``occurred_at`` as a string; UUIDv7 timestamp bits need
    the simulated ms. ``Z`` is normalised to ``+00:00`` for ``fromisoformat``.
    """
    parsed = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def deterministic_injection_id(
    subseed_bytes: bytes,
    mode: ChaosMode,
    event_id: str,
    occurred_at: str,
    instance: int | None = None,
) -> str:
    """The deterministic UUIDv7 ``injection_id`` for one injection (§7.1).

    ``instance`` (the ``duplicate_index``) is supplied for instance-keyed modes
    (``out_of_order``, ``late_arriving``) so per-copy records get distinct ids;
    content-keyed modes pass ``None``.
    """
    full = digest(subseed_bytes, mode, event_id, INJECTION_ID_LABEL, instance)
    random_74 = int.from_bytes(full, "big") & _RANDOM_74_MASK
    uuid_value: UUID = build_uuidv7(timestamp_ms=_occurred_at_ms(occurred_at), random_74=random_74)
    return str(uuid_value)


class InjectionRecord(TypedDict):
    """One ``chaos_injections`` row (§7.1 common fields + mode-specific ``details``).

    ``recorded_at`` is wall-clock and is stamped by the recorder port at insert
    time (it is excluded from golden comparison — §11); the engine leaves it
    absent and the persistence layer fills it. ``workspace_id``/``stream_id``/
    ``shard_id`` come from the :class:`StageContext`.
    """

    injection_id: str
    workspace_id: str
    stream_id: str
    shard_id: int
    mode: ChaosMode
    event_id: str
    sequence_no: int
    occurred_at: str
    canonical_emitted_at: str
    details: dict[str, object]
