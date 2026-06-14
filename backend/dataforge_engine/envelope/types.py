"""Typed shapes for the canonical event envelope ``1.0`` (event-model §2.1, §4, §5).

These ``TypedDict``s are the in-memory representation the engine builds and the
serializer renders. They are deliberately ``total=True`` (every key present —
"absent fields are never permitted in envelope ``1.0``", §2.1) and mirror the
frozen field catalog *in declared order*, which doubles as the canonical
serialization key order (S-2). ``None`` models a JSON ``null`` for the fields the
catalog marks nullable.

Pure Python: no Django, no third-party imports (BE-ENG-1; import-linter
contract 2). ``Decimal`` is the carrier for money / seed / big-int values so the
serializer can render them as strings (S-1/S-6) without float round-trips.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, TypedDict

# A JSON value as it appears *inside* a payload. Monetary / seed / big-int
# scalars are carried as ``Decimal`` and rendered as decimal strings (S-6); every
# other scalar is a native JSON type. Containers nest arbitrarily.
JSONScalar = str | int | float | bool | None | Decimal
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

# The CDC discriminator enum, closed and frozen forever (event-model §2.1 field
# 19, EV-2). ``c`` create, ``u`` update, ``d`` delete, ``r`` snapshot read.
Op = Literal["c", "u", "d", "r"]

# Debezium snapshot marker (event-model §4.2 ``source.snapshot``).
SnapshotMarker = Literal["true", "false", "last"]


class SchemaRef(TypedDict):
    """Field 9 — pointer into the schema registry resolving ``payload`` (§2.1)."""

    subject: str
    version: int


class EntityRef(TypedDict):
    """One entry of field 16 ``entity_refs`` (§2.1)."""

    entity_type: str
    entity_key: str


class CdcSource(TypedDict):
    """The Debezium ``source`` provenance block (event-model §4.2).

    ``ts_ms`` keys are epoch milliseconds (integers); ``tx_id`` is the causing
    business event's ``event_id`` (``None`` for background mutations / snapshots).
    """

    version: str
    connector: str
    name: str
    ts_ms: int
    snapshot: SnapshotMarker
    db: str
    table: str
    seq: int
    entity_version: int
    tx_id: str | None


class CdcPayload(TypedDict):
    """The Debezium-shaped sub-envelope carried as ``payload`` on CDC events
    (event-model §4.1/§4.2). ``before`` is ``null`` for ``c``/``r``; ``after`` is
    ``null`` for ``d``. The payload-level ``op`` must equal the envelope ``op``.
    """

    before: dict[str, JSONValue] | None
    after: dict[str, JSONValue] | None
    op: Op
    ts_ms: int
    source: CdcSource


class DfChaos(TypedDict, total=False):
    """Mode-keyed chaos detail (event-model §5.1). Every key is optional; the
    block carries exactly the key(s) for the mode(s) that touched the instance.
    The internal ``_df`` shape is explicitly *out of* the §8 compatibility
    contract (EV-7) — it may change freely between phases.
    """

    duplicates: dict[str, JSONValue]
    corrupted_values: dict[str, JSONValue]
    nulls: dict[str, JSONValue]
    late_arriving: dict[str, JSONValue]
    schema_drift: dict[str, JSONValue]
    out_of_order: dict[str, JSONValue]


class DfBlock(TypedDict):
    """The internal-only ``_df`` block (event-model §5.1). Present on the ledger
    and internal Kafka; stripped at the delivery boundary (INV-DEL-2).
    """

    canonical: bool
    injection_ids: list[str]
    chaos: DfChaos | None


# ``payload`` is a business document (open dict) or the CDC sub-envelope.
Payload = dict[str, JSONValue] | CdcPayload

# An envelope-shaped mapping the serializer / strip / validator accept: the typed
# ``InternalEnvelope`` / ``DeliveredEnvelope`` ``TypedDict``s, or a plain dict
# (e.g. a JSON-parsed envelope in a round-trip test). ``TypedDict`` is invariant,
# so a read-only ``Mapping[str, object]`` is the widest type that accepts every
# concrete envelope shape without per-call-site casts.
EnvelopeMapping = Mapping[str, object]


class DeliveredEnvelope(TypedDict):
    """The 20-field delivered envelope — what every channel hands to users
    (event-model §2.1, fields 1..20, in canonical serialization order). Produced
    from the internal envelope by ``strip_internal`` (INV-DEL-2).
    """

    envelope_version: str
    event_id: str
    workspace_id: str
    stream_id: str
    shard_id: int
    scenario_slug: str
    manifest_version: str
    event_type: str
    schema_ref: SchemaRef
    sequence_no: int
    partition_key: str
    occurred_at: str
    emitted_at: str
    actor_id: str | None
    session_id: str | None
    entity_refs: list[EntityRef]
    correlation_id: str
    causation_id: str | None
    op: Op | None
    payload: Payload


class InternalEnvelope(DeliveredEnvelope):
    """The internal envelope — the 20 delivered fields plus the internal-only
    ``_df`` block (event-model §2.1 field 21, §5). Lives on the ledger and
    internal Kafka topics.
    """

    _df: DfBlock


# The frozen field catalog in canonical serialization order (event-model §2.1).
# Single source of truth for the serializer (S-2), the field-set pin (CON), and
# the JSON Schema generator (EV-6). ``_df`` is intentionally excluded — it is
# never part of the delivered contract.
DELIVERED_FIELD_ORDER: tuple[str, ...] = (
    "envelope_version",
    "event_id",
    "workspace_id",
    "stream_id",
    "shard_id",
    "scenario_slug",
    "manifest_version",
    "event_type",
    "schema_ref",
    "sequence_no",
    "partition_key",
    "occurred_at",
    "emitted_at",
    "actor_id",
    "session_id",
    "entity_refs",
    "correlation_id",
    "causation_id",
    "op",
    "payload",
)

# The exact 20-key delivered field set, pinned permanently (CON gate; §2.1 / EV-6).
DELIVERED_FIELD_SET: frozenset[str] = frozenset(DELIVERED_FIELD_ORDER)

# The reserved internal-block key and its prefix (event-model §5.2, SB-1). Keys
# beginning with this prefix are reserved at every nesting level and never
# delivered on any channel.
INTERNAL_BLOCK_KEY = "_df"
RESERVED_PREFIX = "_df"

# The frozen envelope contract version (event-model §1, §2.1 field 1).
ENVELOPE_VERSION = "1.0"
