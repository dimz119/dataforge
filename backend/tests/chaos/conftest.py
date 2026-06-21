"""DB-backed fixtures for the chaos durable-buffer tests (Phase 9, §6).

Builds a workspace + verified admin and arms the workspace context (Layer-1
contextvar + Layer-2 GUC) exactly like the runner does via
``worker_workspace_scope`` — so the Class-T RLS WITH CHECK admits the buffer /
injection inserts under the NOBYPASSRLS runtime role (no-op on the SQLite lane).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from dataforge_engine.chaos import InjectionRecord, deterministic_injection_id

STREAM_ID = str(uuid.uuid4())
SHARD_ID = 0
SEED = 424242
_BASE = datetime(2026, 6, 10, 14, 0, 0, tzinfo=UTC)


@dataclass
class ChaosWorld:
    workspace: Any
    admin: Any
    stream_id: str = STREAM_ID
    shard_id: int = SHARD_ID


@pytest.fixture
def chaos_world(db: Any) -> ChaosWorld:
    """A workspace + admin only (runtime-role-safe, no global seeding)."""
    from identity.domain.models import User
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context

    admin = User.objects.create_user(email="chaos-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    workspace = tenancy_services.create_workspace(user=admin, name="Chaos Lab", slug=None)
    ws_context.activate(workspace.id)
    return ChaosWorld(workspace=workspace, admin=admin)


@dataclass
class ChaosApiWorld:
    """A workspace + admin + member + a real Stream + scoped API keys, armed.

    Covers the chaos-policy (api-spec §4.8.3) and answer-key (§4.13) API surfaces:
    JWT clients for the admin and a non-admin member, plus API keys for each relevant
    scope so the auth/scope gating can be exercised end-to-end.
    """

    workspace: Any
    admin: Any
    member: Any
    stream: Any
    stream_id: str
    # plaintext API keys
    answer_key_key: str
    streams_write_key: str
    streams_read_key: str
    noscope_key: str  # a key with only events:read (no streams/answer_key)


@pytest.fixture
def api_world(db: Any) -> ChaosApiWorld:
    """Build the chaos/answer-key API world (a real Stream + scoped keys + members)."""
    from datetime import datetime

    from identity.domain.models import User
    from streams.domain.models import Stream
    from tenancy.application import keys as key_service
    from tenancy.application import services as tenancy_services
    from tenancy.domain import context as ws_context
    from tenancy.domain.models import ROLE_ADMIN, ROLE_MEMBER

    admin = User.objects.create_user(email="ak-admin@example.com", password="pw-correct-horse")
    admin.is_verified = True
    admin.save(update_fields=["is_verified"])
    member = User.objects.create_user(email="ak-member@example.com", password="pw-correct-horse")
    member.is_verified = True
    member.save(update_fields=["is_verified"])

    workspace = tenancy_services.create_workspace(user=admin, name="AK Lab", slug=None)
    ws_context.activate(workspace.id)
    tenancy_services.add_member(
        workspace=workspace, email=member.email, role=ROLE_MEMBER, actor=admin
    )

    stream_id = str(uuid.uuid4())
    stream = Stream.objects.create(
        id=uuid.UUID(stream_id),
        workspace=workspace,
        scenario_config_id=uuid.uuid4(),
        scenario_slug="ecommerce",
        name="ak-stream",
        manifest_version="1.0.0",
        scenario_definition_id=uuid.uuid4(),
        seed=4242,
        created_by=admin.id,
        virtual_epoch=datetime.now(UTC),
    )

    def _mint(name: str, scopes: list[str]) -> str:
        with ws_context.workspace_context(workspace.id):
            _full, plaintext = key_service.create_key(
                workspace=workspace,
                actor=admin,
                name=name,
                scopes=scopes,
                expires_at=None,
                actor_role=ROLE_ADMIN,
            )
        return plaintext

    return ChaosApiWorld(
        workspace=workspace,
        admin=admin,
        member=member,
        stream=stream,
        stream_id=stream_id,
        answer_key_key=_mint("ak-key", ["answer_key:read"]),
        streams_write_key=_mint("write-key", ["streams:write"]),
        streams_read_key=_mint("read-key", ["streams:read"]),
        noscope_key=_mint("noscope-key", ["events:read"]),
    )


def seed_injections(world: ChaosApiWorld, count: int = 3, mode: str = "duplicates") -> None:
    """Insert ``count`` injection rows for the world's stream (RLS-armed)."""
    from datetime import timedelta

    from chaos.domain.models import ChaosInjection
    from tenancy.application.services import worker_workspace_scope

    rows = []
    for i in range(count):
        ts = _BASE + timedelta(seconds=i)
        rows.append(
            ChaosInjection(
                injection_id=uuid.uuid4(),
                workspace_id=world.workspace.id,
                stream_id=uuid.UUID(world.stream_id),
                shard_id=0,
                mode=mode,
                event_id=uuid.uuid4(),
                sequence_no=100 + i,
                occurred_at=ts,
                canonical_emitted_at=ts,
                details={"copies": 1} if mode == "duplicates" else {},
                recorded_at=ts,
            )
        )
    with worker_workspace_scope(world.workspace.id):
        ChaosInjection.objects.bulk_create(rows)


def make_injection(
    workspace_id: str,
    event_id: str,
    *,
    sequence_no: int = 1,
    delay_simulated_ms: int = 1_800_000,
    due_at: str = "2026-06-10T14:30:00.000000Z",
) -> InjectionRecord:
    """One ``late_arriving`` :class:`InjectionRecord` (``outcome: pending``)."""
    occurred = (_BASE).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    injection_id = deterministic_injection_id(
        b"x" * 32, "late_arriving", event_id, occurred, 0
    )
    return {
        "injection_id": injection_id,
        "workspace_id": workspace_id,
        "stream_id": STREAM_ID,
        "shard_id": SHARD_ID,
        "mode": "late_arriving",
        "event_id": event_id,
        "sequence_no": sequence_no,
        "occurred_at": occurred,
        "canonical_emitted_at": occurred,
        "details": {
            "delay_simulated_ms": delay_simulated_ms,
            "due_at_wall": due_at,
            "outcome": "pending",
            "duplicate_index": 0,
        },
    }


def make_entry(injection: InjectionRecord, due_at: str) -> dict[str, Any]:
    """A :class:`ScheduledEntry`-shaped descriptor for the buffer ``schedule``."""
    envelope = {
        "event_id": injection["event_id"],
        "occurred_at": injection["occurred_at"],
        "emitted_at": injection["canonical_emitted_at"],
        "sequence_no": injection["sequence_no"],
        "partition_key": "pk-0",
        "_df": {"canonical": False, "chaos": {"late_arriving": {}}, "injection_ids": []},
    }
    return {
        "workspace_id": injection["workspace_id"],
        "stream_id": injection["stream_id"],
        "shard_id": injection["shard_id"],
        "injection_id": injection["injection_id"],
        "event_id": injection["event_id"],
        "envelope": envelope,
        "due_at": due_at,
        "delay_simulated_ms": injection["details"]["delay_simulated_ms"],
    }
