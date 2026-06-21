"""Scheduled mid-stream upgrade API — the REG-U001..U007 catalog (Exit #4).

api-spec §4.8.4 / schema-registry §10.3: ``POST/GET/DELETE
/streams/{id}/schema-upgrades``. The REG-U001..U007 validation surfaces as a 409
``conflict`` problem with the ``errors[]`` extension (each ``{code, path, message}``);
the happy path is 201 ``scheduled`` → GET list → DELETE 204 cancel, with the cancelled
entry retained. Idempotency-Key replays the same entry. Version skipping (1 → 3) is
legal. Exit criterion #4: each REG-U code rejected with its documented problem code.

REG-U004 (an ``at`` before virtual time) needs a started stream with a virtual clock,
and REG-U005 (a binding that does not resolve against the pinned manifest) needs a
pinned manifest missing the binding context — both arranged on the model directly so
the validator's predicate is exercised through the real HTTP surface.

Runs under the maintenance role (the v2/v3 globals are seeded through Flow 2 and the
``stream_ws`` fixture publishes the global ecommerce scenario).
"""

from __future__ import annotations

import copy
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from streams.domain.models import Stream
from tests.streams.conftest import StreamWorkspaceFixture, create_body

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"


@pytest.fixture
def seeded_evolutions(stream_ws: StreamWorkspaceFixture) -> StreamWorkspaceFixture:
    """``stream_ws`` with order_placed v2/v3 registered via Flow 2."""
    call_command("seed_schema_evolutions", stdout=io.StringIO())
    return stream_ws


def _create_pinned_v1(client: APIClient, stream_ws: StreamWorkspaceFixture) -> str:
    """A stream pinned to order_placed v1 — so v2/v3 are legal upgrade targets."""
    body = create_body(stream_ws, schema_version_pins={_SUBJECT: 1})
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


def _post_upgrade(
    client: APIClient, sid: str, *, subject: str = _SUBJECT, target: int = 2, at: str | None = None
) -> Any:
    body: dict[str, Any] = {"subject": subject, "target_version": target}
    if at is not None:
        body["at"] = at
    return client.post(f"/api/v1/streams/{sid}/schema-upgrades", body, format="json")


def _codes(resp: Any) -> list[str]:
    return [e["code"] for e in resp.json()["errors"]]


# --- the happy path: POST → GET → DELETE --------------------------------------


def test_post_schedules_201(seeded_evolutions: StreamWorkspaceFixture, client: APIClient) -> None:
    """POST a v1→v2 upgrade → 201 with a ``scheduled`` resource."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = _post_upgrade(client, sid, target=2)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["subject"] == _SUBJECT
    assert body["target_version"] == 2
    assert body["status"] == "scheduled"
    assert body["stream_id"] == sid
    assert body["applied_at_wall"] is None and body["cancelled_at"] is None


def test_get_lists_scheduled(seeded_evolutions: StreamWorkspaceFixture, client: APIClient) -> None:
    """GET lists the scheduled entry under the paginated envelope."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    _post_upgrade(client, sid, target=2)
    resp = client.get(f"/api/v1/streams/{sid}/schema-upgrades")
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert len(body["data"]) == 1
    assert body["data"][0]["status"] == "scheduled"


def test_delete_cancels_204_and_retains(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """DELETE a scheduled upgrade → 204; the cancelled entry is retained in the list."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    upgrade_id = _post_upgrade(client, sid, target=2).json()["upgrade_id"]
    resp = client.delete(f"/api/v1/streams/{sid}/schema-upgrades/{upgrade_id}")
    assert resp.status_code == 204
    listed = client.get(f"/api/v1/streams/{sid}/schema-upgrades").json()["data"]
    assert [e["status"] for e in listed] == ["cancelled"]
    assert listed[0]["cancelled_at"] is not None


def test_cancel_non_scheduled_is_409_invalid_state(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """Cancelling an already-cancelled entry → 409 invalid-state-transition."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    upgrade_id = _post_upgrade(client, sid, target=2).json()["upgrade_id"]
    client.delete(f"/api/v1/streams/{sid}/schema-upgrades/{upgrade_id}")
    again = client.delete(f"/api/v1/streams/{sid}/schema-upgrades/{upgrade_id}")
    assert again.status_code == 409
    assert again.json()["type"].endswith("/invalid-state-transition")


def test_cancel_unknown_id_is_404(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """DELETE an unknown upgrade_id → 404."""
    import uuid

    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = client.delete(f"/api/v1/streams/{sid}/schema-upgrades/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_version_skipping_1_to_3_is_legal(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """A v1→v3 skip is accepted (the union of chains, §10.3)."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = _post_upgrade(client, sid, target=3)
    assert resp.status_code == 201, resp.content
    assert resp.json()["target_version"] == 3


def test_idempotency_key_replays_same_entry(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """An Idempotency-Key replay returns the same entry (still 201, no duplicate)."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    body = {"subject": _SUBJECT, "target_version": 2}
    first = client.post(
        f"/api/v1/streams/{sid}/schema-upgrades", body, format="json",
        HTTP_IDEMPOTENCY_KEY="evolve-key-1",
    )
    second = client.post(
        f"/api/v1/streams/{sid}/schema-upgrades", body, format="json",
        HTTP_IDEMPOTENCY_KEY="evolve-key-1",
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["upgrade_id"] == second.json()["upgrade_id"]
    # Exactly one persisted entry.
    listed = client.get(f"/api/v1/streams/{sid}/schema-upgrades").json()["data"]
    assert len(listed) == 1


# --- the REG-U001..U007 rejections, each a 409 with the documented code --------


def test_reg_u001_subject_not_emitted_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U001: a subject the pinned manifest does not emit → 409 with REG-U001."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = _post_upgrade(client, sid, subject="ecommerce.not_emitted", target=2)
    assert resp.status_code == 409
    assert "REG-U001" in _codes(resp)


def test_reg_u002_unregistered_target_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U002: a target version with no registered schema → 409 with REG-U002."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = _post_upgrade(client, sid, target=99)
    assert resp.status_code == 409
    assert "REG-U002" in _codes(resp)


def test_reg_u003_target_not_above_effective_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U003: a target at/below the current effective version → 409 with REG-U003."""
    # Pin to v2; scheduling a v2 (or v1) target is not above effective.
    body = create_body(seeded_evolutions, schema_version_pins={_SUBJECT: 2})
    sid = str(client.post("/api/v1/streams", body, format="json").json()["stream_id"])
    resp = _post_upgrade(client, sid, target=2)
    assert resp.status_code == 409
    assert "REG-U003" in _codes(resp)


def test_reg_u004_at_before_virtual_time_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U004: an explicit ``at`` before the stream's current virtual time → 409."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    # Engage the live clock at a known epoch so virtual_now is well-defined.
    stream = Stream.all_objects.get(id=sid)
    stream.clock_mode = "live"
    stream.virtual_epoch = datetime(2026, 6, 1, tzinfo=UTC)
    stream.speed_multiplier = 1
    stream.first_started_at = datetime.now(UTC) - timedelta(hours=1)
    stream.save(
        update_fields=["clock_mode", "virtual_epoch", "speed_multiplier", "first_started_at"]
    )
    # virtual_now ≈ 2026-06-01 + 1h; an ``at`` well before the epoch is in the past.
    resp = _post_upgrade(client, sid, target=2, at="2026-05-01T00:00:00.000000Z")
    assert resp.status_code == 409
    assert "REG-U004" in _codes(resp)


def test_reg_u005_binding_unresolvable_against_pinned_manifest_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U005: a v2 binding that does not resolve in the pinned manifest → 409.

    The stream is pinned to a manifest snapshot whose actor entity lacks the
    ``address`` attribute, so v2's ``shipping_state`` binding (``actor.address.state``)
    cannot resolve against *this* stream's pinned manifest (REG-U005 validates against
    pinned, not latest).
    """
    sid = _create_pinned_v1(client, seeded_evolutions)
    stream = Stream.all_objects.get(id=sid)
    manifest = copy.deepcopy(stream.pinned_config)
    # Drop the address attribute from the actor entity → the binding context is gone.
    actor = manifest["entities"][manifest["metadata"]["actor_entity"]]
    actor.get("attributes", {}).pop("address", None)
    stream.pinned_config = manifest
    stream.save(update_fields=["pinned_config"])
    resp = _post_upgrade(client, sid, target=2)
    assert resp.status_code == 409
    assert "REG-U005" in _codes(resp)


def test_reg_u006_cdc_subject_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U006: a cdc.* subject can never be upgraded → 409 with REG-U006."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    resp = _post_upgrade(client, sid, subject="ecommerce.cdc.users", target=2)
    assert resp.status_code == 409
    assert "REG-U006" in _codes(resp)


def test_reg_u007_one_scheduled_per_subject_is_409(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """REG-U007: a second scheduled upgrade for the same subject → 409 with REG-U007."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    assert _post_upgrade(client, sid, target=2).status_code == 201
    second = _post_upgrade(client, sid, target=3)
    assert second.status_code == 409
    assert "REG-U007" in _codes(second)


def test_write_requires_scope_foreign_is_404(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient, db: Any
) -> None:
    """A non-member JWT masks the upgrade write to 404 (W-1/W-3)."""
    sid = _create_pinned_v1(client, seeded_evolutions)
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair

    other = User.objects.create_user(email="up-outsider@example.com", password="pw-correct-horse")
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    foreign = APIClient()
    foreign.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(other).access_token}")
    assert _post_upgrade(foreign, sid, target=2).status_code == 404
