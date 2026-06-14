"""Build + validate helper tests (event-model §2, §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from dataforge_engine.envelope import (
    ENVELOPE_VERSION,
    CdcPayload,
    InternalEnvelope,
    build_cdc_payload,
    build_cdc_source,
    build_internal_envelope,
    make_canonical_df,
    make_schema_ref,
)
from dataforge_engine.envelope.build import EnvelopeBuildError, validate_envelope_field_set
from dataforge_engine.envelope.timestamps import emitted_at_ms, occurred_at_ms

from .fixtures import (
    STREAM_ID,
    WORKSPACE_ID,
    SeededRandomBits,
    order_placed_envelope,
)

_OCC = datetime(2026, 6, 10, 16, 2, 41, 9314, tzinfo=UTC)
_EMIT = datetime(2026, 6, 10, 16, 2, 41, 155002, tzinfo=UTC)


def test_make_schema_ref_subject_form() -> None:
    ref = make_schema_ref("ecommerce", "order_placed", 1)
    assert ref == {"subject": "ecommerce.order_placed", "version": 1}


def test_make_schema_ref_cdc_subject_form() -> None:
    ref = make_schema_ref("ecommerce", "cdc.users", 1)
    assert ref == {"subject": "ecommerce.cdc.users", "version": 1}


def test_make_schema_ref_rejects_zero_version() -> None:
    with pytest.raises(EnvelopeBuildError):
        make_schema_ref("ecommerce", "order_placed", 0)


def test_business_envelope_field_set() -> None:
    env = order_placed_envelope()
    validate_envelope_field_set(env)
    assert env["envelope_version"] == ENVELOPE_VERSION
    assert env["op"] is None
    assert env["_df"]["canonical"] is True


def _cdc_envelope() -> InternalEnvelope:
    """A ``cdc.users`` (op=u) background-mutation envelope (event-model §7.2)."""
    source = build_cdc_source(
        name=f"dataforge.{WORKSPACE_ID}",
        occurred_at_ms=occurred_at_ms(_OCC),
        emitted_at_ms=emitted_at_ms(_EMIT),
        snapshot="false",
        db="ecommerce",
        table="users",
        seq=51077,
        entity_version=7,
        tx_id=None,
    )
    payload = build_cdc_payload(
        op="u",
        before={"user_id": "usr_a3f81c2e9b4d", "city": "Columbus"},
        after={"user_id": "usr_a3f81c2e9b4d", "city": "Austin"},
        emitted_at_ms=emitted_at_ms(_EMIT),
        source=source,
    )
    return build_internal_envelope(
        event_id="019ea2d8-1f3a-7b5c-9d0e-4a6b8c0d2e4f",
        workspace_id=WORKSPACE_ID,
        stream_id=STREAM_ID,
        shard_id=0,
        scenario_slug="ecommerce",
        manifest_version="1.0.0",
        event_type="cdc.users",
        schema_ref=make_schema_ref("ecommerce", "cdc.users", 1),
        sequence_no=51077,
        partition_entity_type="users",
        partition_entity_key="usr_a3f81c2e9b4d",
        occurred_at=_OCC,
        emitted_at=_EMIT,
        actor_id=None,
        session_id=None,
        entity_refs=[{"entity_type": "users", "entity_key": "usr_a3f81c2e9b4d"}],
        correlation_id="019ea2d8-1f3a-7b5c-9d0e-4a6b8c0d2e4f",
        causation_id=None,
        op="u",
        payload=payload,
        df=make_canonical_df(),
    )


def test_cdc_envelope_op_equality_enforced() -> None:
    env = _cdc_envelope()
    assert env["op"] == "u"
    payload = cast("CdcPayload", env["payload"])
    assert payload["op"] == "u"
    assert payload["source"]["ts_ms"] == occurred_at_ms(_OCC)
    assert payload["ts_ms"] == emitted_at_ms(_EMIT)
    assert payload["source"]["version"] == ENVELOPE_VERSION


def test_cdc_create_must_have_null_before() -> None:
    src = build_cdc_source(
        name="dataforge.x", occurred_at_ms=1, emitted_at_ms=2, snapshot="false",
        db="ecommerce", table="orders", seq=1, entity_version=1, tx_id=None,
    )
    with pytest.raises(EnvelopeBuildError):
        build_cdc_payload(op="c", before={"x": 1}, after={"x": 1}, emitted_at_ms=2, source=src)


def test_cdc_delete_must_have_null_after() -> None:
    src = build_cdc_source(
        name="dataforge.x", occurred_at_ms=1, emitted_at_ms=2, snapshot="false",
        db="ecommerce", table="orders", seq=1, entity_version=2, tx_id=None,
    )
    with pytest.raises(EnvelopeBuildError):
        build_cdc_payload(op="d", before={"x": 1}, after={"x": 1}, emitted_at_ms=2, source=src)


def test_cdc_payload_op_must_equal_envelope_op() -> None:
    src = build_cdc_source(
        name="dataforge.x", occurred_at_ms=1, emitted_at_ms=2, snapshot="false",
        db="ecommerce", table="orders", seq=1, entity_version=1, tx_id=None,
    )
    payload = build_cdc_payload(op="c", before=None, after={"x": 1}, emitted_at_ms=2, source=src)
    with pytest.raises(EnvelopeBuildError):
        build_internal_envelope(
            event_id=order_placed_envelope()["event_id"],
            workspace_id=WORKSPACE_ID, stream_id=STREAM_ID, shard_id=0,
            scenario_slug="ecommerce", manifest_version="1.0.0", event_type="cdc.orders",
            schema_ref=make_schema_ref("ecommerce", "cdc.orders", 1), sequence_no=1,
            partition_entity_type="orders", partition_entity_key="ord_1",
            occurred_at=_OCC, emitted_at=_EMIT, actor_id=None, session_id=None,
            entity_refs=[{"entity_type": "orders", "entity_key": "ord_1"}],
            correlation_id="019ea2d8-1f3a-7b5c-9d0e-4a6b8c0d2e4f", causation_id=None,
            op="u",  # mismatch: payload op is "c"
            payload=payload, df=make_canonical_df(),
        )


def test_empty_entity_refs_rejected() -> None:
    with pytest.raises(EnvelopeBuildError):
        build_internal_envelope(
            event_id=event_id_seed(), workspace_id=WORKSPACE_ID, stream_id=STREAM_ID,
            shard_id=0, scenario_slug="ecommerce", manifest_version="1.0.0",
            event_type="order_placed", schema_ref=make_schema_ref("ecommerce", "order_placed", 1),
            sequence_no=1, partition_entity_type="users", partition_entity_key="usr_1",
            occurred_at=_OCC, emitted_at=_EMIT, actor_id="usr_1", session_id=None,
            entity_refs=[], correlation_id="019ea2d8-1f3a-7b5c-9d0e-4a6b8c0d2e4f",
            causation_id=None, op=None, payload={"x": 1}, df=make_canonical_df(),
        )


def test_sequence_no_must_be_positive() -> None:
    with pytest.raises(EnvelopeBuildError):
        build_internal_envelope(
            event_id=event_id_seed(), workspace_id=WORKSPACE_ID, stream_id=STREAM_ID,
            shard_id=0, scenario_slug="ecommerce", manifest_version="1.0.0",
            event_type="order_placed", schema_ref=make_schema_ref("ecommerce", "order_placed", 1),
            sequence_no=0, partition_entity_type="users", partition_entity_key="usr_1",
            occurred_at=_OCC, emitted_at=_EMIT, actor_id="usr_1", session_id=None,
            entity_refs=[{"entity_type": "users", "entity_key": "usr_1"}],
            correlation_id="019ea2d8-1f3a-7b5c-9d0e-4a6b8c0d2e4f",
            causation_id=None, op=None, payload={"x": 1}, df=make_canonical_df(),
        )


def event_id_seed() -> str:
    from dataforge_engine.envelope import event_id_for

    return event_id_for(_OCC, SeededRandomBits(1))
