"""``partition_key`` derivation (event-model §2.2.3, rules PK-1..3).

Frozen definition::

    partition_key = "{workspace_id}:{stream_id}:{partition_entity_type}:{partition_entity_key}"

``workspace_id`` is the mandatory first segment (ADR-0002): every internal Kafka
message is keyed by a workspace-prefixed key, making tenant attribution
inspectable at the broker. Components never legitimately contain ``:`` (UUIDs,
validated slugs, entity keys ≤ 64 chars), so the key is unambiguous — this module
*enforces* that with a hard reject, turning a contract assumption into a runtime
guard.

Derivation by event class:

* PK-1 — business event: partition entity = the entity named by the event type's
  manifest ``partition_by`` (default = the actor's root entity). The engine
  resolves ``partition_by`` to a concrete ``(entity_type, entity_key)`` and
  passes it here; this module does not read the manifest.
* PK-2 — CDC event: partition entity = the mutated entity itself
  (``{entity_type}:{entity_key}`` of the ``before``/``after`` images). Not
  overridable.
* PK-3 — snapshot read (``op == "r"``): same as PK-2.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

_SEP = ":"
# The full key is bounded at 256 chars (event-model §2.1 field 11); entity keys
# at 64 (§2.2.3). We enforce the field-11 cap here as the last line of defence.
_MAX_PARTITION_KEY_LEN = 256


class PartitionKeyError(ValueError):
    """Raised when a partition-key component is malformed (contains ``:`` / empty)
    or the assembled key exceeds the 256-char bound (event-model §2.1 field 11).
    """


def _require_clean(component: str, name: str) -> str:
    if component == "":
        raise PartitionKeyError(f"partition-key component {name!r} is empty")
    if _SEP in component:
        raise PartitionKeyError(
            f"partition-key component {name!r}={component!r} contains the reserved "
            f"separator {_SEP!r} (event-model §2.2.3)"
        )
    return component


def derive_partition_key(
    *,
    workspace_id: str,
    stream_id: str,
    partition_entity_type: str,
    partition_entity_key: str,
) -> str:
    """Assemble the frozen ``partition_key`` from its four resolved components.

    Applies to all three rules — the caller selects the partition entity per
    PK-1/PK-2/PK-3 and this function performs the identical, unambiguous join +
    validation. Raises :class:`PartitionKeyError` on a malformed component or an
    over-long key.
    """
    parts = (
        _require_clean(workspace_id, "workspace_id"),
        _require_clean(stream_id, "stream_id"),
        _require_clean(partition_entity_type, "partition_entity_type"),
        _require_clean(partition_entity_key, "partition_entity_key"),
    )
    key = _SEP.join(parts)
    if len(key) > _MAX_PARTITION_KEY_LEN:
        raise PartitionKeyError(
            f"partition_key length {len(key)} exceeds the {_MAX_PARTITION_KEY_LEN}-char "
            "bound (event-model §2.1 field 11)"
        )
    return key
