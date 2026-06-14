"""Build + validate helpers for the canonical envelope (event-model §2, §4).

These are the constructors the generation pipeline (Phase 4) calls to assemble a
well-formed internal envelope from already-resolved inputs, plus the cross-field
invariant checks that catch a malformed build *before* it reaches the ledger.

What this module does NOT do: resolve the manifest ``partition_by``, draw seeds,
or stamp wall-clock time — those are the caller's (the engine's) responsibility.
It assembles the frozen shape, in the frozen order, and enforces the structural
invariants that span fields (op ↔ payload, schema_ref subject form, entity_refs
non-empty, CDC ``op`` equality). Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .partition import derive_partition_key
from .types import (
    DELIVERED_FIELD_ORDER,
    ENVELOPE_VERSION,
    CdcPayload,
    DfBlock,
    InternalEnvelope,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .types import (
        CdcSource,
        DfChaos,
        EntityRef,
        EnvelopeMapping,
        JSONValue,
        Op,
        Payload,
        SchemaRef,
        SnapshotMarker,
    )


class EnvelopeBuildError(ValueError):
    """Raised when assembled inputs would violate a frozen envelope invariant."""


def make_schema_ref(scenario_slug: str, event_type: str, version: int) -> SchemaRef:
    """``schema_ref`` with the derived subject (INV-REG-1): subject =
    ``{scenario_slug}.{event_type}`` (CDC event types already carry the
    ``cdc.{entity}`` form in ``event_type``, so this composes correctly for both).
    """
    if version < 1:
        raise EnvelopeBuildError(f"schema_ref.version must be >= 1, got {version}")
    return {"subject": f"{scenario_slug}.{event_type}", "version": version}


def make_canonical_df(injection_ids: list[str] | None = None) -> DfBlock:
    """The ``_df`` block of an untouched canonical instance (event-model §5.1):
    ``canonical=true``, ``injection_ids=[]``, ``chaos=null``. This is the value
    every ledger row carries (always canonical).
    """
    return {
        "canonical": True,
        "injection_ids": list(injection_ids) if injection_ids else [],
        "chaos": None,
    }


def make_df(canonical: bool, injection_ids: list[str], chaos: DfChaos | None) -> DfBlock:
    """Assemble an arbitrary ``_df`` block (chaos artifacts; event-model §5.1)."""
    return {
        "canonical": canonical,
        "injection_ids": list(injection_ids),
        "chaos": chaos,
    }


def build_cdc_source(
    *,
    name: str,
    occurred_at_ms: int,
    emitted_at_ms: int,
    snapshot: SnapshotMarker,
    db: str,
    table: str,
    seq: int,
    entity_version: int,
    tx_id: str | None,
) -> CdcSource:
    """The Debezium ``source`` block (event-model §4.2). ``source.ts_ms`` is the
    *simulated* change time (= ``occurred_at`` ms); the wall-clock ``ts_ms`` lives
    at the payload level (:func:`build_cdc_payload`).
    """
    if entity_version < 1:
        raise EnvelopeBuildError(f"source.entity_version must be >= 1, got {entity_version}")
    return {
        "version": ENVELOPE_VERSION,
        "connector": "dataforge",
        "name": name,
        "ts_ms": occurred_at_ms,
        "snapshot": snapshot,
        "db": db,
        "table": table,
        "seq": seq,
        "entity_version": entity_version,
        "tx_id": tx_id,
    }


def build_cdc_payload(
    *,
    op: Op,
    before: dict[str, JSONValue] | None,
    after: dict[str, JSONValue] | None,
    emitted_at_ms: int,
    source: CdcSource,
) -> CdcPayload:
    """Assemble + validate the Debezium-shaped CDC ``payload`` (event-model §4).

    Enforces the §4.3 before/after rules: ``before`` is ``null`` for ``c``/``r``;
    ``after`` is ``null`` for ``d``. The payload-level ``op`` is set to the given
    ``op`` and must later equal the envelope ``op`` (checked in
    :func:`build_internal_envelope`).
    """
    if op in ("c", "r") and before is not None:
        raise EnvelopeBuildError(f"CDC op={op!r} must have before=null (event-model §4.3)")
    if op == "d" and after is not None:
        raise EnvelopeBuildError("CDC op='d' must have after=null (event-model §4.3)")
    if op in ("c", "u", "r") and after is None:
        raise EnvelopeBuildError(f"CDC op={op!r} must have a non-null after image (§4.3)")
    if op in ("u", "d") and before is None:
        raise EnvelopeBuildError(f"CDC op={op!r} must have a non-null before image (§4.3)")
    return {
        "before": before,
        "after": after,
        "op": op,
        "ts_ms": emitted_at_ms,
        "source": source,
    }


def build_internal_envelope(
    *,
    event_id: str,
    workspace_id: str,
    stream_id: str,
    shard_id: int,
    scenario_slug: str,
    manifest_version: str,
    event_type: str,
    schema_ref: SchemaRef,
    sequence_no: int,
    partition_entity_type: str,
    partition_entity_key: str,
    occurred_at: datetime,
    emitted_at: datetime,
    actor_id: str | None,
    session_id: str | None,
    entity_refs: list[EntityRef],
    correlation_id: str,
    causation_id: str | None,
    op: Op | None,
    payload: Payload,
    df: DfBlock,
) -> InternalEnvelope:
    """Assemble a fully-validated internal envelope in canonical field order.

    Derives ``partition_key`` from the resolved partition entity (PK-1..3 are the
    caller's choice of entity), formats both timestamps to the pinned RFC 3339
    form, and enforces the cross-field invariants (op ↔ payload discriminator,
    CDC ``op`` equality, non-empty ``entity_refs``). The dict is built in the
    §2.1 order so insertion order already matches canonical serialization order.
    """
    from .timestamps import format_rfc3339  # local: keep module import graph flat

    if sequence_no < 1:
        raise EnvelopeBuildError(f"sequence_no must be >= 1, got {sequence_no}")
    if shard_id < 0:
        raise EnvelopeBuildError(f"shard_id must be >= 0, got {shard_id}")
    if not entity_refs:
        raise EnvelopeBuildError("entity_refs must never be empty (event-model §2.1 field 16)")

    _validate_op_payload(op, payload)

    partition_key = derive_partition_key(
        workspace_id=workspace_id,
        stream_id=stream_id,
        partition_entity_type=partition_entity_type,
        partition_entity_key=partition_entity_key,
    )

    envelope: InternalEnvelope = {
        "envelope_version": ENVELOPE_VERSION,
        "event_id": event_id,
        "workspace_id": workspace_id,
        "stream_id": stream_id,
        "shard_id": shard_id,
        "scenario_slug": scenario_slug,
        "manifest_version": manifest_version,
        "event_type": event_type,
        "schema_ref": schema_ref,
        "sequence_no": sequence_no,
        "partition_key": partition_key,
        "occurred_at": format_rfc3339(occurred_at),
        "emitted_at": format_rfc3339(emitted_at),
        "actor_id": actor_id,
        "session_id": session_id,
        "entity_refs": entity_refs,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "op": op,
        "payload": payload,
        "_df": df,
    }
    return envelope


def _validate_op_payload(op: Op | None, payload: Payload) -> None:
    """``op == null`` ⇔ business event; non-null ⇔ CDC event whose payload is the
    §4 sub-envelope, and whose payload-level ``op`` must equal the envelope ``op``
    (event-model §2.1 field 19, §4.1).
    """
    if op is None:
        if isinstance(payload, dict) and payload.get("op") in ("c", "u", "d", "r"):
            # A business payload that happens to carry a Debezium-shaped op is a
            # generation bug — the discriminator must be authoritative.
            if {"before", "after", "source"} <= payload.keys():
                raise EnvelopeBuildError(
                    "business event (op=null) carries a CDC-shaped payload (§2.1 field 19)"
                )
        return
    # CDC event: payload must be the sub-envelope and its op must equal envelope op.
    payload_op = payload.get("op") if isinstance(payload, dict) else None
    if payload_op != op:
        raise EnvelopeBuildError(
            f"CDC payload op={payload_op!r} must equal envelope op={op!r} (event-model §4.1)"
        )


def validate_envelope_field_set(envelope: EnvelopeMapping) -> None:
    """Assert an envelope mapping carries exactly the canonical field set.

    Internal envelopes carry the 20 delivered fields plus ``_df``; delivered
    envelopes carry exactly the 20. Used by the CON pin test and as a cheap
    builder self-check. Raises :class:`EnvelopeBuildError` on a missing field.
    """
    for field in DELIVERED_FIELD_ORDER:
        if field not in envelope:
            raise EnvelopeBuildError(f"envelope missing required field {field!r} (§2.1)")
