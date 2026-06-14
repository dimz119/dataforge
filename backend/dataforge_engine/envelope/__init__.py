"""Canonical event envelope ``1.0`` — the framework-free library (event-model D5).

Public API for the frozen envelope contract: typed shapes, deterministic
``event_id`` (UUIDv7 under §2.2.1 determinism), canonical byte-stable
serialization (S-1..S-6), ``partition_key`` derivation (PK-1..3), the ``_df``
strip boundary (INV-DEL-2), build + validate helpers, the JSON Schema generator
(the CI artifact source, EV-6), and schema validation.

Pure Python: zero Django / DRF / Celery / Channels / redis / confluent_kafka /
psycopg imports (BE-ENG-1; import-linter contract 2 is CI-blocking).

Downstream import paths (stable):

    from dataforge_engine.envelope import (
        # shapes
        InternalEnvelope, DeliveredEnvelope, SchemaRef, EntityRef,
        CdcPayload, CdcSource, DfBlock, DfChaos, Op, SnapshotMarker,
        DELIVERED_FIELD_ORDER, DELIVERED_FIELD_SET, ENVELOPE_VERSION,
        # event_id
        event_id_for, build_uuidv7, RandomBitsSource,
        # serialize
        canonical_serialize, canonical_serialize_str,
        # partition
        derive_partition_key,
        # strip
        strip_internal,
        # build
        build_internal_envelope, build_cdc_payload, build_cdc_source,
        make_schema_ref, make_canonical_df, make_df, validate_envelope_field_set,
        # schema
        generate_envelope_schema,
        validate_envelope, is_valid_envelope, iter_envelope_errors,
        validate_against_schema,
        # timestamps
        format_rfc3339, to_epoch_ms, occurred_at_ms, emitted_at_ms,
    )
"""

from __future__ import annotations

from .build import (
    EnvelopeBuildError,
    build_cdc_payload,
    build_cdc_source,
    build_internal_envelope,
    make_canonical_df,
    make_df,
    make_schema_ref,
    validate_envelope_field_set,
)
from .event_id import EventIdError, RandomBitsSource, build_uuidv7, event_id_for
from .partition import PartitionKeyError, derive_partition_key
from .schema_gen import generate_envelope_schema
from .serialize import (
    SerializationError,
    canonical_serialize,
    canonical_serialize_str,
)
from .strip import StripError, strip_internal
from .timestamps import (
    TimestampError,
    emitted_at_ms,
    format_rfc3339,
    occurred_at_ms,
    to_epoch_ms,
)
from .types import (
    DELIVERED_FIELD_ORDER,
    DELIVERED_FIELD_SET,
    ENVELOPE_VERSION,
    INTERNAL_BLOCK_KEY,
    RESERVED_PREFIX,
    CdcPayload,
    CdcSource,
    DeliveredEnvelope,
    DfBlock,
    DfChaos,
    EntityRef,
    EnvelopeMapping,
    InternalEnvelope,
    JSONValue,
    Op,
    Payload,
    SchemaRef,
    SnapshotMarker,
)
from .validate import (
    EnvelopeSchemaError,
    is_valid_envelope,
    iter_envelope_errors,
    validate_against_schema,
    validate_envelope,
)

__all__ = [
    # shapes / constants
    "DELIVERED_FIELD_ORDER",
    "DELIVERED_FIELD_SET",
    "ENVELOPE_VERSION",
    "INTERNAL_BLOCK_KEY",
    "RESERVED_PREFIX",
    "CdcPayload",
    "CdcSource",
    "DeliveredEnvelope",
    "DfBlock",
    "DfChaos",
    "EntityRef",
    # errors
    "EnvelopeBuildError",
    "EnvelopeMapping",
    "EnvelopeSchemaError",
    "EventIdError",
    "InternalEnvelope",
    "JSONValue",
    "Op",
    "PartitionKeyError",
    "Payload",
    # event_id
    "RandomBitsSource",
    "SchemaRef",
    "SerializationError",
    "SnapshotMarker",
    "StripError",
    "TimestampError",
    # build
    "build_cdc_payload",
    "build_cdc_source",
    "build_internal_envelope",
    "build_uuidv7",
    # serialize
    "canonical_serialize",
    "canonical_serialize_str",
    # partition
    "derive_partition_key",
    # timestamps
    "emitted_at_ms",
    "event_id_for",
    "format_rfc3339",
    # schema
    "generate_envelope_schema",
    "is_valid_envelope",
    "iter_envelope_errors",
    "make_canonical_df",
    "make_df",
    "make_schema_ref",
    "occurred_at_ms",
    # strip
    "strip_internal",
    "to_epoch_ms",
    "validate_against_schema",
    "validate_envelope",
    "validate_envelope_field_set",
]
