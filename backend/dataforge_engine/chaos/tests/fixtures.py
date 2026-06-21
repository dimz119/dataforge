"""Deterministic test fixtures for the chaos engine (framework-free).

Builds canonical ``InternalEnvelope`` instances with reproducible ``event_id``s
(UUIDv7 with seeded random bits) and clean ``_df`` blocks, plus a batch
generator. No Django, no DB — these ride the pure unit lane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from dataforge_engine.chaos import StageContext, chaos_subseed
from dataforge_engine.chaos.context import InMemoryRecorder
from dataforge_engine.envelope import InternalEnvelope, build_uuidv7, make_canonical_df
from dataforge_engine.envelope.types import Op, Payload

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
STREAM_ID = "22222222-2222-2222-2222-222222222222"
SHARD_ID = 0
SEED = 424242

_BASE = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)


def make_envelope(
    sequence_no: int,
    *,
    event_type: str = "order_placed",
    payload: Payload | None = None,
) -> InternalEnvelope:
    """A clean canonical envelope with a deterministic UUIDv7 ``event_id``."""
    occurred = _BASE + timedelta(milliseconds=sequence_no)
    ts_ms = int(occurred.timestamp() * 1000)
    event_id = str(build_uuidv7(timestamp_ms=ts_ms, random_74=sequence_no * 7919))
    body: Payload = payload if payload is not None else cast(
        Payload,
        {"total": "64.97", "subtotal": "59.97", "quantity": 3, "currency": "USD", "paid": True},
    )
    envelope: InternalEnvelope = {
        "envelope_version": "1.0",
        "event_id": event_id,
        "workspace_id": WORKSPACE_ID,
        "stream_id": STREAM_ID,
        "shard_id": SHARD_ID,
        "scenario_slug": "shop",
        "manifest_version": "1.0.0",
        "event_type": event_type,
        "schema_ref": {"subject": f"shop.{event_type}", "version": 1},
        "sequence_no": sequence_no,
        "partition_key": f"pk-{sequence_no % 8}",
        "occurred_at": occurred.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "emitted_at": occurred.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "actor_id": None,
        "session_id": None,
        "entity_refs": [{"entity_type": "order", "entity_key": f"ord-{sequence_no}"}],
        "correlation_id": event_id,
        "causation_id": None,
        "op": None,
        "payload": body,
        "_df": make_canonical_df(),
    }
    return envelope


def make_batch(n: int, *, event_type: str = "order_placed") -> list[InternalEnvelope]:
    """``n`` clean canonical envelopes, ``sequence_no`` 1..n."""
    return [make_envelope(i, event_type=event_type) for i in range(1, n + 1)]


def make_context(
    recorder: InMemoryRecorder | None = None,
    *,
    registry_view: object | None = None,
    virtual_clock: object | None = None,
) -> StageContext:
    """A :class:`StageContext` bound to the fixture seed + in-memory recorder."""
    return StageContext(
        stream_id=STREAM_ID,
        shard_id=SHARD_ID,
        workspace_id=WORKSPACE_ID,
        chaos_subseed=chaos_subseed(SEED),
        recorder=recorder if recorder is not None else InMemoryRecorder(),
        registry_view=registry_view,
        virtual_clock=virtual_clock,
    )


class FakeDriftMenu:
    """A test drift menu (DR-1): a next version + its added fields for one subject."""

    def __init__(
        self, from_version: int, to_version: int, added_fields: list[dict[str, object]]
    ) -> None:
        self.from_version = from_version
        self.to_version = to_version
        self.added_fields = added_fields


class FakeRegistryView:
    """An in-memory ``registry_view`` port (DR-1): subject → next-version menu.

    Mirrors the Postgres-backed snapshot the Django ``chaos`` app supplies; tests
    inject the v2 menu directly so the engine never touches a DB.
    """

    def __init__(self, menus: dict[str, FakeDriftMenu]) -> None:
        self._menus = menus

    def menu_for(self, subject: str) -> FakeDriftMenu | None:
        return self._menus.get(subject)


class FakeVirtualClock:
    """An in-memory ``virtual_clock`` port (§5.6): the shard's virtual epoch ms."""

    def __init__(self, virtual_epoch_ms: int) -> None:
        self.virtual_epoch_ms = virtual_epoch_ms


def base_epoch_ms() -> int:
    """Epoch ms of the fixture ``_BASE`` instant (the window anchor for tests)."""
    return int(_BASE.timestamp() * 1000)


def make_cdc_envelope(
    sequence_no: int,
    *,
    op: str = "u",
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    event_type: str = "users",
) -> InternalEnvelope:
    """A CDC-shaped envelope (Debezium sub-envelope as ``payload``) for drift tests."""
    cdc_payload = cast(
        Payload,
        {
            "before": before,
            "after": after,
            "op": op,
            "ts_ms": 0,
            "source": {},
        },
    )
    env = make_envelope(sequence_no, event_type=event_type, payload=cdc_payload)
    env["op"] = cast("Op", op)
    env["schema_ref"] = {"subject": f"cdc.{event_type}", "version": 1}
    return env
