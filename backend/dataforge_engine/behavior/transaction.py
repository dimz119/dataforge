"""PoolTransaction — one mutation, two views (behavior-engine §6.1; ADR-0012).

Every transition's effects run inside a :class:`PoolTransaction`. On commit it
produces the canonical envelopes for the pass: the business event first (if the
transition declares ``emit``), then one CDC event per mutation of a CDC-enabled
entity whose ``op`` is listed in the entity's ``cdc.ops`` — consecutive
``sequence_no``s, same ``occurred_at`` (R-CDC-2). Abort discards the transaction
wholesale (BE-G5): no partial mutation is ever visible to a later timer.

The CDC view is **derived now** (``entity_version`` bumped on every mutation,
before/after images captured) so Phase 8 derives without rework; the cdc.* EVENTS
are emitted from this phase since the ledger needs them for referential proof and
the SCD2 exercise. The hook seam where a future CDC *projection/filter* layer
would sit is marked "Phase 8".

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dataforge_engine.envelope import (
    build_cdc_payload,
    build_cdc_source,
    build_internal_envelope,
    make_canonical_df,
    make_schema_ref,
)

if TYPE_CHECKING:
    from datetime import datetime

    from dataforge_engine.envelope import InternalEnvelope
    from dataforge_engine.envelope.types import JSONValue, Op, SnapshotMarker

    from .ir import ManifestIR


@dataclass(frozen=True)
class StreamIdentity:
    """The pinned per-stream constants the transaction stamps on every envelope."""

    workspace_id: str
    stream_id: str
    shard_id: int
    scenario_slug: str
    manifest_version: str


@dataclass
class Mutation:
    """One captured pool mutation (behavior-engine §6.1 step 1).

    ``event_id`` is pre-minted by the interpreter from the traversal's ``values``
    cursor (§7.1: "event_id digests for in-session events"), so all RNG stays in
    one place and the CDC id is replay-stable.
    """

    entity_type: str
    entity_key: str
    op: Op
    before: dict[str, JSONValue] | None
    after: dict[str, JSONValue] | None
    entity_version: int
    event_id: str


class SequenceCounter:
    """The gapless per-(stream, shard) ``sequence_no`` counter (INV-GEN-7).

    Continues across stop/restart (never resets, T12); checkpointed as
    ``sequence_no_last`` (§9.1). ``next()`` returns the next value (≥ 1).
    """

    __slots__ = ("_last",)

    def __init__(self, last: int = 0) -> None:
        self._last = last

    @property
    def last(self) -> int:
        return self._last

    def next(self) -> int:
        self._last += 1
        return self._last

    def reset_to(self, last: int) -> None:
        """Resume the counter at ``last`` (checkpoint restore; INV-GEN-7)."""
        self._last = last


class PoolTransaction:
    """Collects mutations + an optional business event, then builds envelopes.

    Effects register mutations via :meth:`record_mutation` in declaration order;
    :meth:`commit` assigns sequence numbers and builds the envelopes. The caller
    (interpreter) supplies the business event details before commit via
    :meth:`set_business_event`.
    """

    def __init__(
        self,
        ir: ManifestIR,
        identity: StreamIdentity,
        *,
        occurred_at: datetime,
        emitted_at: datetime,
    ) -> None:
        self._ir = ir
        self._id = identity
        self._occurred_at = occurred_at
        self._emitted_at = emitted_at
        self._mutations: list[Mutation] = []
        self._business: _BusinessEvent | None = None

    def record_mutation(self, mutation: Mutation) -> None:
        self._mutations.append(mutation)

    def set_business_event(
        self,
        *,
        event_type: str,
        event_id: str,
        partition_entity_type: str,
        partition_entity_key: str,
        actor_id: str | None,
        session_id: str | None,
        entity_refs: list[dict[str, str]],
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, JSONValue],
    ) -> None:
        self._business = _BusinessEvent(
            event_type=event_type,
            event_id=event_id,
            partition_entity_type=partition_entity_type,
            partition_entity_key=partition_entity_key,
            actor_id=actor_id,
            session_id=session_id,
            entity_refs=entity_refs,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=payload,
        )

    def commit(self, sequence: SequenceCounter) -> list[InternalEnvelope]:
        """Assign sequence numbers and build the business + CDC envelopes."""
        envelopes: list[InternalEnvelope] = []
        tx_id: str | None = None
        correlation_id: str
        if self._business is not None:
            biz = self._build_business(sequence.next())
            tx_id = self._business.event_id
            correlation_id = self._business.correlation_id
            envelopes.append(biz)
        else:
            # CDC-only transaction (background mutation, R-CDC-3): chain root is the
            # first CDC event's own id; handled inside _build_cdc when no business
            # event exists.
            correlation_id = ""
        for mutation in self._mutations:
            cdc = self._maybe_build_cdc(
                mutation, sequence, tx_id=tx_id, correlation_id=correlation_id
            )
            if cdc is not None:
                envelopes.append(cdc)
        return envelopes

    # -- builders -----------------------------------------------------------

    def _build_business(self, seq: int) -> InternalEnvelope:
        assert self._business is not None
        biz = self._business
        return build_internal_envelope(
            event_id=biz.event_id,
            workspace_id=self._id.workspace_id,
            stream_id=self._id.stream_id,
            shard_id=self._id.shard_id,
            scenario_slug=self._id.scenario_slug,
            manifest_version=self._id.manifest_version,
            event_type=biz.event_type,
            schema_ref=make_schema_ref(
                self._id.scenario_slug, biz.event_type,
                self._ir.schema_versions.get(biz.event_type, 1),
            ),
            sequence_no=seq,
            partition_entity_type=biz.partition_entity_type,
            partition_entity_key=biz.partition_entity_key,
            occurred_at=self._occurred_at,
            emitted_at=self._emitted_at,
            actor_id=biz.actor_id,
            session_id=biz.session_id,
            entity_refs=[
                {"entity_type": r["entity_type"], "entity_key": r["entity_key"]}
                for r in biz.entity_refs
            ],
            correlation_id=biz.correlation_id,
            causation_id=biz.causation_id,
            op=None,
            payload=biz.payload,
            df=make_canonical_df(),
        )

    def _maybe_build_cdc(
        self, mutation: Mutation, sequence: SequenceCounter,
        *, tx_id: str | None, correlation_id: str,
    ) -> InternalEnvelope | None:
        entity = self._ir.entities[mutation.entity_type]
        if not entity.cdc_enabled or mutation.op not in entity.cdc_ops:
            # Mutation still happened; non-CDC entities emit nothing (R-CDC-M1).
            # Phase 8: a CDC projection/filter layer would also gate here.
            return None
        return self._build_cdc(mutation, sequence, tx_id=tx_id, correlation_id=correlation_id)

    def _build_cdc(
        self, mutation: Mutation, sequence: SequenceCounter,
        *, tx_id: str | None, correlation_id: str,
    ) -> InternalEnvelope:
        from dataforge_engine.envelope.timestamps import emitted_at_ms, occurred_at_ms

        seq = sequence.next()
        # event_id is pre-minted by the interpreter from the traversal values
        # cursor (§7.1), so the CDC id is replay-stable.
        cdc_event_id = mutation.event_id
        snapshot: SnapshotMarker = "true" if mutation.op == "r" else "false"
        source = build_cdc_source(
            name=f"dataforge.{self._id.workspace_id}",
            occurred_at_ms=occurred_at_ms(self._occurred_at),
            emitted_at_ms=emitted_at_ms(self._emitted_at),
            snapshot=snapshot,
            db=self._id.scenario_slug,
            table=mutation.entity_type,
            seq=seq,
            entity_version=mutation.entity_version,
            tx_id=tx_id,
        )
        payload = build_cdc_payload(
            op=mutation.op,
            before=mutation.before,
            after=mutation.after,
            emitted_at_ms=emitted_at_ms(self._emitted_at),
            source=source,
        )
        is_background = tx_id is None and not correlation_id
        corr = cdc_event_id if is_background else (correlation_id or cdc_event_id)
        return build_internal_envelope(
            event_id=cdc_event_id,
            workspace_id=self._id.workspace_id,
            stream_id=self._id.stream_id,
            shard_id=self._id.shard_id,
            scenario_slug=self._id.scenario_slug,
            manifest_version=self._id.manifest_version,
            event_type=f"cdc.{mutation.entity_type}",
            schema_ref=make_schema_ref(
                self._id.scenario_slug, f"cdc.{mutation.entity_type}",
                self._ir.schema_versions.get(f"cdc.{mutation.entity_type}", 1),
            ),
            sequence_no=seq,
            partition_entity_type=mutation.entity_type,
            partition_entity_key=mutation.entity_key,
            occurred_at=self._occurred_at,
            emitted_at=self._emitted_at,
            actor_id=None if is_background else self._business_actor(),
            session_id=None if is_background else self._business_session(),
            entity_refs=[
                {"entity_type": mutation.entity_type, "entity_key": mutation.entity_key}
            ],
            correlation_id=corr,
            causation_id=None if is_background else tx_id,
            op=mutation.op,
            payload=payload,
            df=make_canonical_df(),
        )

    def _business_actor(self) -> str | None:
        return self._business.actor_id if self._business else None

    def _business_session(self) -> str | None:
        return self._business.session_id if self._business else None


@dataclass
class _BusinessEvent:
    event_type: str
    event_id: str
    partition_entity_type: str
    partition_entity_key: str
    actor_id: str | None
    session_id: str | None
    entity_refs: list[dict[str, str]]
    correlation_id: str
    causation_id: str | None
    payload: dict[str, JSONValue]
