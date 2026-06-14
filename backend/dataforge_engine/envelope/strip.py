"""The delivery-boundary strip (event-model §5.2, INV-DEL-2, SB-1/SB-2).

Every sink calls :func:`strip_internal` exactly once at ingest (SB-2): it removes
the internal ``_df`` block — and, defensively, *any* top-level key beginning with
the reserved ``_df`` prefix (SB-1: the prefix is reserved at every nesting level)
— and returns the delivered envelope: exactly the 20 fields of §2.1.

The result is the single shared shape buffer rows store, WS frames carry, and
all future sinks transmit. A sink that persists or transmits an unstripped
envelope is a release-blocking defect (SB-2); the permanent contract scan (SB-3)
proves no reserved-prefix key escapes on any channel.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .types import DELIVERED_FIELD_SET, RESERVED_PREFIX

if TYPE_CHECKING:
    from .types import DeliveredEnvelope, EnvelopeMapping


class StripError(ValueError):
    """Raised when an envelope cannot be stripped to exactly the 20 delivered keys
    (a missing required field, i.e. a malformed internal envelope).
    """


def strip_internal(envelope: EnvelopeMapping) -> DeliveredEnvelope:
    """Strip ``_df`` (and any reserved-prefix key) → the 20-field delivered envelope.

    Idempotent: stripping an already-delivered envelope returns an equivalent
    delivered envelope. Raises :class:`StripError` if any of the 20 required
    delivered fields is absent — a malformed internal envelope is a bug, never
    delivered (event-model §2.1: all 20 keys present in envelope 1.0).
    """
    delivered = {
        key: value
        for key, value in dict(envelope).items()
        if not key.startswith(RESERVED_PREFIX)
    }
    missing = DELIVERED_FIELD_SET - delivered.keys()
    if missing:
        raise StripError(
            "internal envelope is missing required delivered field(s): "
            f"{sorted(missing)} (event-model §2.1)"
        )
    extra = delivered.keys() - DELIVERED_FIELD_SET
    if extra:
        # Forward-compat: unknown non-``_df`` keys are tolerated by consumers
        # (S-4), but the canonical delivered shape this strip produces is exactly
        # the pinned set, so an unexpected non-reserved key is a builder bug.
        raise StripError(
            f"envelope carries unexpected non-reserved key(s): {sorted(extra)} "
            "(expected exactly the 20 delivered fields; event-model §2.1)"
        )
    return cast("DeliveredEnvelope", delivered)
