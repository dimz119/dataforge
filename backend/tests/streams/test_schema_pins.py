"""Per-stream schema pins + the effective-version projection (Exit #1).

schema-registry §10.1-10.2: a stream pins each subject's schema version. PIN-R1 —
an empty ``schema_version_pins`` materializes to each subject's latest-at-first-start
(resolved once, then carried in the checkpoint); PIN-R2 — an explicit entry overrides
per subject; PIN-R3 — create-time validation (every key a subject the manifest emits,
every value a registered version) → 422; §10.2 — ``effective = max(materialized pin,
highest applied upgrade target)``. ``GET /streams/{id}/schema-versions`` returns
``{effective, pending, applied}``.

The materialized-pin-in-checkpoint persistence is pinned by writing a checkpoint
``runtime`` side-car (the runner's PIN-R1 freeze) and asserting the endpoint reads it
authoritatively, overriding the pre-start preview.

Runs under the maintenance role (the v2/v3 globals are seeded through Flow 2 and the
``stream_ws`` fixture publishes the global ecommerce scenario).
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from streams.application import schema_pins
from tests.streams.conftest import StreamWorkspaceFixture, create_body

pytestmark = pytest.mark.django_db

_SUBJECT = "ecommerce.order_placed"


@pytest.fixture
def seeded_evolutions(stream_ws: StreamWorkspaceFixture) -> StreamWorkspaceFixture:
    """The ``stream_ws`` workspace with v2/v3 of order_placed registered (Flow 2)."""
    call_command("seed_schema_evolutions", stdout=io.StringIO())
    return stream_ws


def _create(client: APIClient, stream_ws: StreamWorkspaceFixture, **kw: Any) -> str:
    resp = client.post("/api/v1/streams", create_body(stream_ws, **kw), format="json")
    assert resp.status_code == 201, resp.content
    return str(resp.json()["stream_id"])


def _write_runtime_checkpoint(
    *, workspace_id: str, stream_id: str, runtime: dict[str, Any]
) -> None:
    """Insert a shard-0 checkpoint whose blob carries the §10.2 ``runtime`` side-car.

    Mirrors ``runner.checkpoint_store.CheckpointStore.save(runtime=)`` — the engine
    codec ignores the ``runtime`` key, so a minimal blob suffices for the read path
    ``schema_pins._load_runtime`` exercises.
    """
    from datetime import UTC, datetime

    from generation.domain.models import StreamCheckpoint
    from generation.infra.compression import compress

    blob = {"runtime": runtime}
    StreamCheckpoint.all_objects.create(
        workspace_id=workspace_id,
        stream_id=stream_id,
        shard_id=0,
        checkpoint_seq=1,
        fencing_token=1,
        state=compress(json.dumps(blob).encode("utf-8")),
        state_format=1,
        last_sequence_no=100,
        virtual_clock_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- PIN-R1 / PIN-R2: materialization -----------------------------------------


def _pinned_manifest(client: APIClient, stream_ws: StreamWorkspaceFixture) -> dict[str, Any]:
    """The created stream's snapshotted manifest (``pinned_config``) — the pin context."""
    from streams.domain.models import Stream

    sid = _create(client, stream_ws)
    return dict(Stream.all_objects.get(id=sid).pinned_config or {})


def test_pin_r1_empty_materializes_to_latest(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """PIN-R1: an empty pin map materializes each emitted subject to its latest version."""
    manifest = _pinned_manifest(client, seeded_evolutions)
    materialized = schema_pins.materialize_pins({}, manifest=manifest)
    # order_placed has v1/v2/v3 registered → latest is 3.
    assert materialized[_SUBJECT] == 3
    # Every materialized subject is a subject the manifest emits.
    assert all(v >= 1 for v in materialized.values())


def test_pin_r2_explicit_overrides_per_subject(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """PIN-R2: an explicit ``{subject: 1}`` overrides the latest-default for that subject."""
    manifest = _pinned_manifest(client, seeded_evolutions)
    materialized = schema_pins.materialize_pins({_SUBJECT: 1}, manifest=manifest)
    assert materialized[_SUBJECT] == 1  # pinned below latest (the evolution exercise)


# --- PIN-R3: create-time validation -------------------------------------------


def test_pin_r3_unknown_subject_is_422(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """PIN-R3: a pin for a subject the manifest does not emit → 422 with errors[]."""
    body = create_body(seeded_evolutions, schema_version_pins={"ecommerce.not_a_subject": 1})
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 422, resp.content
    problem = resp.json()
    assert resp["Content-Type"] == "application/problem+json"
    codes = [e["code"] for e in problem["errors"]]
    assert codes == ["PIN-R3"]


def test_pin_r3_unregistered_version_is_422(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """PIN-R3: pinning a version above the latest registered → 422."""
    body = create_body(seeded_evolutions, schema_version_pins={_SUBJECT: 99})
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 422, resp.content
    assert [e["code"] for e in resp.json()["errors"]] == ["PIN-R3"]


def test_valid_explicit_pin_creates(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """A pin at a registered version (v1) is accepted and surfaced on the resource."""
    body = create_body(seeded_evolutions, schema_version_pins={_SUBJECT: 1})
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 201, resp.content
    # The additive Stream-resource field previews the explicit pin (pre-start).
    assert resp.json()["schema_versions"][_SUBJECT] == 1


# --- §10.2: effective = max(pin, applied) -------------------------------------


def test_effective_is_max_pin_and_applied() -> None:
    """§10.2: effective folds the materialized pin with the highest applied upgrade."""
    # pin v1, an applied upgrade to v2 → effective v2.
    assert schema_pins.effective_versions({_SUBJECT: 1}, {_SUBJECT: 2}) == {_SUBJECT: 2}
    # pin v2 already above an applied v2 stays v2 (max).
    assert schema_pins.effective_versions({_SUBJECT: 2}, {_SUBJECT: 2}) == {_SUBJECT: 2}
    # no applied upgrades → the pin is effective.
    assert schema_pins.effective_versions({_SUBJECT: 1}, {}) == {_SUBJECT: 1}


# --- the schema-versions endpoint ---------------------------------------------


def test_schema_versions_endpoint_shape(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """GET /schema-versions returns {effective, pending, applied} (pre-start preview)."""
    sid = _create(client, seeded_evolutions, schema_version_pins={_SUBJECT: 1})
    resp = client.get(f"/api/v1/streams/{sid}/schema-versions")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert set(body) == {"effective", "pending", "applied"}
    assert body["effective"][_SUBJECT] == 1  # the explicit pin preview
    assert body["pending"] == [] and body["applied"] == []


def test_pin_r1_materialized_pin_in_checkpoint_is_authoritative(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """PIN-R1: once materialized into the checkpoint, the frozen pin overrides the preview.

    A stream created with no explicit pin previews ``latest`` (v3). After first start
    the runner freezes the materialized pin into the checkpoint ``runtime``; the
    endpoint must then read that frozen value (here v1 — as if latest had been v1 at
    start) regardless of later registrations, the §10.2 source of truth.
    """
    sid = _create(client, seeded_evolutions)  # no explicit pin → preview = latest (v3)
    preview = client.get(f"/api/v1/streams/{sid}/schema-versions").json()
    assert preview["effective"][_SUBJECT] == 3  # the pre-start preview is the latest

    # The runner froze the pin at v1 at first start (materialized-once, PIN-R1).
    _write_runtime_checkpoint(
        workspace_id=str(seeded_evolutions.workspace.id),
        stream_id=sid,
        runtime={"schema_pins": {_SUBJECT: 1}, "applied_upgrades": {}},
    )
    after = client.get(f"/api/v1/streams/{sid}/schema-versions").json()
    assert after["effective"][_SUBJECT] == 1  # the checkpoint pin wins (authoritative)


def test_effective_folds_applied_upgrade_from_checkpoint(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient
) -> None:
    """§10.2: a checkpoint with pin v1 + an applied v2 upgrade surfaces effective v2."""
    sid = _create(client, seeded_evolutions)
    _write_runtime_checkpoint(
        workspace_id=str(seeded_evolutions.workspace.id),
        stream_id=sid,
        runtime={"schema_pins": {_SUBJECT: 1}, "applied_upgrades": {_SUBJECT: 2}},
    )
    body = client.get(f"/api/v1/streams/{sid}/schema-versions").json()
    assert body["effective"][_SUBJECT] == 2  # max(pin 1, applied 2)


def test_schema_versions_foreign_credential_is_404(
    seeded_evolutions: StreamWorkspaceFixture, client: APIClient, db: Any
) -> None:
    """A non-member JWT masks the schema-versions endpoint to 404 (W-1/W-3)."""
    sid = _create(client, seeded_evolutions)
    from identity.domain.models import User
    from identity.infra.jwt import issue_token_pair

    other = User.objects.create_user(email="pin-outsider@example.com", password="pw-correct-horse")
    other.is_verified = True
    other.save(update_fields=["is_verified"])
    foreign = APIClient()
    foreign.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(other).access_token}")
    assert foreign.get(f"/api/v1/streams/{sid}/schema-versions").status_code == 404
