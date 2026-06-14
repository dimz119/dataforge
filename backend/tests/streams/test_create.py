"""Stream create tests (api-spec §4.8 #39; T1, INV-STR-5).

create copies the instance pin (manifest_version + merged config + sha256 + config
revision), fixes the seed (client-supplied or server-generated, immutable), seeds
the MVP shard row, and creates the stream as created/desired-stopped.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from streams.domain.models import (
    LC_CREATED,
    MVP_SHARD_ID,
    RUN_STOPPED,
    Stream,
    StreamShard,
)
from tests.streams.conftest import StreamWorkspaceFixture, create_body


@pytest.mark.django_db
def test_create_copies_pin_and_fixes_seed(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    resp = client.post("/api/v1/streams", create_body(stream_ws), format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["status"] == LC_CREATED
    assert body["desired_state"]["run_state"] == RUN_STOPPED
    assert body["scenario_slug"] == "ecommerce"
    assert body["manifest_version"] == "1.0.0"
    assert body["config_revision"] == stream_ws.instance.config_version
    assert body["seed"] == "424242424242"  # client-supplied, preserved
    assert body["pin_sha256"]  # the determinism fingerprint is computed (PIN-1)
    assert resp["Location"] == f"/api/v1/streams/{body['stream_id']}"

    # The pin was COPIED (a snapshot), not referenced: the merged config is stored.
    stream = Stream.all_objects.get(id=body["stream_id"])
    assert stream.pinned_config  # merged manifest+overlay snapshot copied at create
    assert stream.seed == 424242424242
    assert stream.scenario_config_id == stream_ws.instance.id
    # The MVP shard row exists with fencing_token 0.
    shard = StreamShard.all_objects.get(stream_id=stream.id, shard_id=MVP_SHARD_ID)
    assert shard.fencing_token == 0


@pytest.mark.django_db
def test_create_generates_seed_when_omitted(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    body = create_body(stream_ws)
    del body["seed"]
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 201, resp.content
    seed = int(resp.json()["seed"])
    assert 0 <= seed <= (2**63) - 1  # the R-3 domain


@pytest.mark.django_db
def test_create_default_target_tps_is_10(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    body = create_body(stream_ws)
    del body["target_tps"]
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["desired_state"]["target_tps"] == 10


@pytest.mark.django_db
def test_create_foreign_instance_404(
    client: APIClient, stream_ws: StreamWorkspaceFixture
) -> None:
    import uuid

    body = create_body(stream_ws, scenario_instance_id=str(uuid.uuid4()))
    resp = client.post("/api/v1/streams", body, format="json")
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_create_audits(client: APIClient, stream_ws: StreamWorkspaceFixture) -> None:
    from audit.domain.models import AuditLog

    resp = client.post("/api/v1/streams", create_body(stream_ws), format="json")
    assert resp.status_code == 201
    entry = AuditLog.objects.filter(action="streams.stream.created").first()
    assert entry is not None
    assert entry.metadata["pin_sha256"] == resp.json()["pin_sha256"]
