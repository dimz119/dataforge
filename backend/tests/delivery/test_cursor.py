"""REST cursor pull tests (delivery-channels §5; database-schema §6.1; security §3.3).

Two tiers:

* **Pure codec** (``test_cursor_*``): the ``c1.`` encode/decode round-trip, the
  byte-identity property the replay contract leans on, fingerprint binding (wrong
  stream / filter set → ``cursor-invalid``), and the §5.4 expiry predicate. These
  run in any lane (no DB).
* **Live-DB endpoint** (``test_events_*``): the full ``GET /streams/{id}/events``
  surface over a populated ``event_buffer`` — replay byte-identity, ``410
  cursor-expired`` with ``earliest_cursor`` (logical floor + dropped partition),
  ``400 cursor-invalid``, X-API-Key ``events:read`` enforcement, foreign-workspace
  404 masking, and the exactly-20-key delivered output (no ``_df``). They run on the
  SQLite unit lane (plain ``event_buffer`` table, no RLS) and the Postgres lane
  (RLS + hourly partitions); the partition-drop expiry case is Postgres-only and
  skips elsewhere. The verify agent owns the compose/Postgres runtime (Phase-5 CI
  note).

Buffer rows are written through the real :class:`BufferWriterChannel` (the §4 sink)
so the stored shape is exactly the delivered 20-key envelope the endpoint returns —
the test exercises the real write→read path, not a hand-rolled fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rest_framework.test import APIClient

from delivery.domain.cursor import (
    CURSOR_MAX_LEN,
    CURSOR_PREFIX,
    CursorDecodeError,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
)

EVENTS_URL = "/api/v1/streams/{sid}/events"


# ===========================================================================
# Pure codec tests (no DB)
# ===========================================================================


def _fp(stream_id: str = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b", filters: str = "") -> str:
    return filter_fingerprint(stream_id=stream_id, canonical_filter_set=filters)


def test_cursor_round_trip() -> None:
    """encode → decode returns the same position + verifies the fingerprint (§5.2)."""
    fp = _fp()
    token = encode_cursor(p=1781193785287, s=48214, fingerprint=fp)
    assert token.startswith(CURSOR_PREFIX)
    pos = decode_cursor(token, expected_fingerprint=fp)
    assert (pos.p, pos.s, pos.f) == (1781193785287, 48214, fp)


def test_cursor_matches_spec_example() -> None:
    """The §5.1 worked example cursor is reproduced byte-for-byte (encoding is normative)."""
    fp = "375e3c19"
    token = encode_cursor(p=1781193785287, s=48214, fingerprint=fp)
    assert token == "c1.eyJmIjoiMzc1ZTNjMTkiLCJwIjoxNzgxMTkzNzg1Mjg3LCJzIjo0ODIxNH0"


def test_cursor_byte_identity_on_reencode() -> None:
    """Re-encoding a decoded position yields the identical token (replay stability)."""
    fp = _fp()
    token = encode_cursor(p=42, s=7, fingerprint=fp)
    pos = decode_cursor(token, expected_fingerprint=fp)
    assert encode_cursor(p=pos.p, s=pos.s, fingerprint=pos.f) == token


def test_cursor_is_opaque_and_bounded() -> None:
    """A cursor is a URL-safe token ≤ 128 chars (RC-7 opacity bound)."""
    token = encode_cursor(p=2**52, s=2**52, fingerprint=_fp())
    assert len(token) <= CURSOR_MAX_LEN
    assert "=" not in token  # base64url without padding


def test_cursor_wrong_fingerprint_is_rejected() -> None:
    """A cursor presented against a different stream/filter set → fingerprint error (RC-8)."""
    token = encode_cursor(p=1, s=1, fingerprint=_fp(filters=""))
    with pytest.raises(CursorDecodeError) as exc:
        decode_cursor(token, expected_fingerprint=_fp(filters="order_placed"))
    assert exc.value.kind == "fingerprint"


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-cursor",
        "c2.eyJ9",  # unknown version prefix
        "c1.",  # empty body
        "c1.@@@notbase64@@@",
        "c1." + "A" * 200,  # over the opacity bound
    ],
)
def test_cursor_malformed_is_format_error(bad: str) -> None:
    """Undecodable / unknown-prefix / over-long tokens → format error (RC-8)."""
    with pytest.raises(CursorDecodeError) as exc:
        decode_cursor(bad, expected_fingerprint=_fp())
    assert exc.value.kind == "format"


def test_filter_fingerprint_is_canonical_order_independent() -> None:
    """Sorted-deduped filter sets map to one fingerprint (RC-4) via the service helper."""
    from delivery.application.services import canonical_filter_set

    a = canonical_filter_set(("b", "a", "a"))
    b = canonical_filter_set(("a", "b"))
    assert a == b == "a,b"
    assert _fp(filters=a) == _fp(filters=b)


def test_expiry_predicate() -> None:
    """The §5.4 expiry predicate: below the logical floor OR the physical floor."""
    from delivery.infra import buffer_reader

    assert buffer_reader.is_expired(cursor_p=10, retention_floor=100, physical_floor=None)
    assert not buffer_reader.is_expired(cursor_p=200, retention_floor=100, physical_floor=None)
    # Past the physical floor (dropped partition) even if above the logical floor.
    assert buffer_reader.is_expired(cursor_p=150, retention_floor=100, physical_floor=180)
    assert not buffer_reader.is_expired(cursor_p=150, retention_floor=100, physical_floor=120)


# ===========================================================================
# Live-DB endpoint tests
# ===========================================================================


@pytest.fixture
def cursor_world(db: Any) -> Any:
    """A workspace + admin + events:read key + a Stream + N buffer rows, armed.

    Writes buffer rows through the real BufferWriterChannel so the stored shape is
    the delivered 20-key envelope; returns a small fixture object the endpoint tests
    drive. Mirrors the buffer-writer Postgres conftest's arming so RLS passes under
    the NOBYPASSRLS role on the Postgres lane.
    """
    from datetime import datetime as _dt

    from dataforge_engine.envelope.tests.fixtures import order_placed_envelope
    from delivery.infra.buffer_writer_channel import BufferWriterChannel
    from identity.domain.models import User
    from streams.domain.models import Stream
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.application.services import worker_workspace_scope
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN
    from tests.delivery.conformance import make_batch

    admin = User.objects.create_user(email="cursor-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Cursor Lab", slug=None)
    ws_context.activate(workspace.id)

    ws_id = str(workspace.id)
    stream_id = str(uuid.uuid4())
    Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name="cursor-stream",
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=_dt.now(UTC),
    )

    with ws_context.workspace_context(workspace.id):
        _full_key, plaintext = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name="cursor-key",
            scopes=["events:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
        _no_scope_key, no_scope_plain = key_service.create_key(
            workspace=workspace,
            actor=admin,
            name="no-events-key",
            scopes=["streams:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )

    # Write 12 delivered rows (distinct event_ids) through the real sink.
    events = []
    for i in range(12):
        env = dict(order_placed_envelope(seed=4242 + i))
        env["workspace_id"] = ws_id
        env["stream_id"] = stream_id
        events.append(env)
    batch = make_batch(events, workspace_id=ws_id, stream_id=stream_id)  # type: ignore[arg-type]
    with worker_workspace_scope(workspace.id):
        result = BufferWriterChannel().deliver(batch)
    assert result.status == "ok", result.error

    class World:
        pass

    world = World()
    world.workspace = workspace  # type: ignore[attr-defined]
    world.admin = admin  # type: ignore[attr-defined]
    world.stream_id = stream_id  # type: ignore[attr-defined]
    world.key = plaintext  # type: ignore[attr-defined]
    world.no_scope_key = no_scope_plain  # type: ignore[attr-defined]
    world.row_count = 12  # type: ignore[attr-defined]
    return world


def _key_client(key: str) -> APIClient:
    api = APIClient()
    api.credentials(HTTP_X_API_KEY=key)
    return api


@pytest.mark.django_db
def test_events_first_page_and_pagination(cursor_world: Any) -> None:
    """from=earliest reads in buffer order; the cursor pages to exhaustion (§5.3)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    resp = api.get(url, {"from": "earliest", "limit": "5"})
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert len(body["data"]) == 5
    assert body["next_cursor"] is not None
    assert body["next_cursor"].startswith(CURSOR_PREFIX)

    # Page 2 + 3 walk the rest; total == row_count, no overlap.
    seen = [e["event_id"] for e in body["data"]]
    cursor = body["next_cursor"]
    for _ in range(5):
        page = api.get(url, {"cursor": cursor, "limit": "5"}).json()
        seen.extend(e["event_id"] for e in page["data"])
        cursor = page["next_cursor"]
        if not page["data"]:
            break
    assert len(seen) == cursor_world.row_count
    assert len(set(seen)) == cursor_world.row_count  # gap-free, no dupes


@pytest.mark.django_db
def test_events_replay_is_byte_identical(cursor_world: Any) -> None:
    """Re-reading the same cursor returns a byte-identical response (INV-DEL-3)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    first = api.get(url, {"from": "earliest", "limit": "7"})
    assert first.status_code == 200, first.content
    again = api.get(url, {"from": "earliest", "limit": "7"})
    assert again.content == first.content  # byte-identical


@pytest.mark.django_db
def test_events_output_is_exactly_20_keys_no_df(cursor_world: Any) -> None:
    """Every delivered envelope has exactly the 20 contract fields, no _df (SB-3)."""
    from dataforge_engine.envelope import DELIVERED_FIELD_SET
    from tests.delivery.conformance import assert_no_reserved_prefix

    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    data = api.get(url, {"from": "earliest", "limit": "50"}).json()["data"]
    assert data
    for env in data:
        assert len(env) == 20
        assert set(env.keys()) == set(DELIVERED_FIELD_SET)
        assert_no_reserved_prefix(env)


@pytest.mark.django_db
def test_events_tail_page_keeps_cursor(cursor_world: Any) -> None:
    """An empty tail page returns the same position, never null/204 (RC-2/RC-3)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    tail = api.get(url, {"from": "latest"}).json()
    assert tail["data"] == []
    assert tail["next_cursor"] is not None
    again = api.get(url, {"cursor": tail["next_cursor"]}).json()
    assert again["data"] == []
    assert again["next_cursor"] == tail["next_cursor"]  # cursor does not move


@pytest.mark.django_db
def test_events_410_on_expired_cursor_logical_floor(cursor_world: Any) -> None:
    """A cursor past the plan retention floor → 410 cursor-expired + earliest_cursor."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    # Mint a cursor whose p is 100 days in the past (well past 24h Free retention),
    # fingerprint-bound to this stream + empty filter set.
    fp = filter_fingerprint(stream_id=cursor_world.stream_id, canonical_filter_set="")
    ancient = datetime.now(UTC) - timedelta(days=100)
    ancient_ms = int(ancient.timestamp() * 1000)
    expired = encode_cursor(p=ancient_ms, s=1, fingerprint=fp)

    resp = api.get(url, {"cursor": expired})
    assert resp.status_code == 410, resp.content
    body = resp.json()
    assert body["type"].endswith("/cursor-expired")
    assert body["status"] == 410
    assert "earliest_cursor" in body
    assert body["earliest_cursor"].startswith(CURSOR_PREFIX)
    assert body["retention_hours"] in (24, 48)
    # The earliest_cursor is itself usable (recovery is one request away, §5.4).
    recovered = api.get(url, {"cursor": body["earliest_cursor"], "limit": "3"})
    assert recovered.status_code == 200, recovered.content
    assert len(recovered.json()["data"]) == 3


@pytest.mark.django_db
def test_events_410_on_dropped_partition(cursor_world: Any) -> None:
    """A cursor below the oldest attached partition floor → 410 (physical drop, §5.4).

    Postgres-only: the physical-floor check reads the partition catalog. On SQLite
    there is no partition machinery, so this asserts the logical floor alone is
    skipped and the Postgres lane covers the dropped-partition path.
    """
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("dropped-partition expiry requires PostgreSQL partitions (compose/CI lane).")

    from delivery.infra import partitions

    # Ensure recent partitions exist, then compute a p below the oldest attached
    # partition's lower bound but ABOVE the (48h) logical floor — pure physical drop.
    now = datetime.now(UTC)
    with connection.cursor() as cur:
        partitions.ensure_partitions(cur, start=now, hours_ahead=1)
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    fp = filter_fingerprint(stream_id=cursor_world.stream_id, canonical_filter_set="")
    # An hour far below any attached partition (the conftest only touched ~now).
    long_ago = now - timedelta(hours=200)
    p = int(long_ago.timestamp() * 1000)
    resp = api.get(url, {"cursor": encode_cursor(p=p, s=1, fingerprint=fp)})
    assert resp.status_code == 410, resp.content
    assert resp.json()["type"].endswith("/cursor-expired")


@pytest.mark.django_db
def test_events_400_on_bad_cursor(cursor_world: Any) -> None:
    """An undecodable cursor → 400 cursor-invalid (distinct from expiry, RC-8)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    resp = api.get(url, {"cursor": "c1.not-valid-base64-@@@"})
    assert resp.status_code == 400, resp.content
    assert resp.json()["type"].endswith("/cursor-invalid")


@pytest.mark.django_db
def test_events_400_on_wrong_stream_fingerprint(cursor_world: Any) -> None:
    """A cursor minted for a different stream → 400 cursor-invalid (fingerprint, RC-8)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    other_fp = filter_fingerprint(stream_id=str(uuid.uuid4()), canonical_filter_set="")
    foreign_cursor = encode_cursor(p=1, s=1, fingerprint=other_fp)
    resp = api.get(url, {"cursor": foreign_cursor})
    assert resp.status_code == 400, resp.content
    assert resp.json()["type"].endswith("/cursor-invalid")


@pytest.mark.django_db
def test_events_400_on_filter_set_mismatch(cursor_world: Any) -> None:
    """A cursor created under one filter set, presented with another → 400 (RC-4)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    # Get a real cursor under the unfiltered set, then present it WITH a types filter.
    first = api.get(url, {"from": "earliest", "limit": "3"}).json()
    resp = api.get(url, {"cursor": first["next_cursor"], "types": "order_placed"})
    assert resp.status_code == 400, resp.content
    assert resp.json()["type"].endswith("/cursor-invalid")


@pytest.mark.django_db
def test_events_types_filter_narrows_without_renumbering(cursor_world: Any) -> None:
    """types= narrows delivery; an unknown type matches nothing (RC-4)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    matched = api.get(url, {"from": "earliest", "limit": "50", "types": "order_placed"}).json()
    assert len(matched["data"]) == cursor_world.row_count  # all rows are order_placed
    none = api.get(url, {"from": "earliest", "limit": "50", "types": "no_such_type"}).json()
    assert none["data"] == []


@pytest.mark.django_db
def test_events_requires_events_read_scope(cursor_world: Any) -> None:
    """A key lacking events:read (within its own workspace) → 403 permission-denied."""
    api = _key_client(cursor_world.no_scope_key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    resp = api.get(url, {"from": "earliest"})
    assert resp.status_code == 403, resp.content
    body = resp.json()
    assert body["type"].endswith("/permission-denied")
    assert body.get("required_scope") == "events:read"


@pytest.mark.django_db
def test_events_no_credential_is_401(cursor_world: Any) -> None:
    """No X-API-Key / Authorization → 401 (security §3.3)."""
    resp = APIClient().get(EVENTS_URL.format(sid=cursor_world.stream_id), {"from": "earliest"})
    assert resp.status_code == 401, resp.content


@pytest.mark.django_db
def test_events_foreign_key_is_404(cursor_world: Any, db: Any) -> None:
    """A valid key from another workspace → 404 masking (never 403, RC-5/W-1)."""
    from identity.domain.models import User
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN

    other_admin = User.objects.create_user(
        email="foreigner@example.com", password="pw-correct-horse"
    )
    other_admin.is_verified = True
    other_admin.save(update_fields=["is_verified"])
    other_ws = tenancy_services.create_workspace(user=other_admin, name="Foreign", slug=None)
    with ws_context.workspace_context(other_ws.id):
        _key, foreign_plain = key_service.create_key(
            workspace=other_ws,
            actor=other_admin,
            name="foreign-key",
            scopes=["events:read"],
            expires_at=None,
            actor_role=ROLE_ADMIN,
        )
    ws_context.activate(cursor_world.workspace.id)

    resp = _key_client(foreign_plain).get(
        EVENTS_URL.format(sid=cursor_world.stream_id), {"from": "earliest"}
    )
    assert resp.status_code == 404, resp.content  # never 403 (anti-enumeration)


@pytest.mark.django_db
def test_events_unknown_stream_is_404(cursor_world: Any) -> None:
    """An absent stream id masks identically to a foreign one → 404 (W-3)."""
    api = _key_client(cursor_world.key)
    resp = api.get(EVENTS_URL.format(sid=uuid.uuid4()), {"from": "earliest"})
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_events_cursor_and_from_mutually_exclusive(cursor_world: Any) -> None:
    """Presenting both cursor and from → 400 validation-error (§5.1)."""
    api = _key_client(cursor_world.key)
    url = EVENTS_URL.format(sid=cursor_world.stream_id)
    first = api.get(url, {"from": "earliest", "limit": "1"}).json()
    resp = api.get(url, {"cursor": first["next_cursor"], "from": "earliest"})
    assert resp.status_code == 400, resp.content
