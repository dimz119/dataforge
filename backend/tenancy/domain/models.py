"""Domain models for the Tenancy context (domain-model §2.2).

Table names are fixed by database-schema.md and pinned via ``Meta.db_table``
(rule BE-APP-1 / C-2). UUID pks are app-generated (C-3). Enumerations are
``text`` + named ``CHECK`` (C-5). Timestamps are ``timestamptz`` UTC (C-4).

Tenant-owned models (``memberships``, ``workspace_invitations``, ``api_keys``,
``workspace_quotas``, ``usage_counters``) subclass ``WorkspaceScopedModel`` so
they carry a non-null ``workspace_id``, the scoped default manager, and the
declarative tenant marker (security §4.1). ``Workspace`` is *self*-tenant-owned
(its ``id`` is the tenant id, §9.4) — it is scoped by RLS Class W and by
membership checks, not by a ``workspace_id`` column, so it is **not** a
``WorkspaceScopedModel`` and is listed as such in ``tenancy_exempt``.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Length, Lower
from django.utils import timezone

from tenancy.domain.ids import uuid4, uuid7
from tenancy.domain.scoping import WorkspaceScopedModel

# Register the ``__length`` transform on TextField so the char_length CHECK
# constraints (database-schema §3.3/§3.6) express in the ORM as the DDL does.
models.TextField.register_lookup(Length)

# --- shared value objects ----------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_CHOICES: list[tuple[str, str]] = [(ROLE_ADMIN, "Admin"), (ROLE_MEMBER, "Member")]

PLAN_FREE = "free"
PLAN_CHOICES: list[tuple[str, str]] = [
    (PLAN_FREE, "Free"),
    ("classroom", "Classroom"),
    ("pro", "Pro"),
]

# KeyScope vocabulary (domain-model §2.2; database-schema §3.6 CHECK).
SCOPE_EVENTS_READ = "events:read"
SCOPE_STREAMS_READ = "streams:read"
SCOPE_STREAMS_WRITE = "streams:write"
SCOPE_SCHEMAS_READ = "schemas:read"
SCOPE_ANSWER_KEY_READ = "answer_key:read"
KEY_SCOPES: tuple[str, ...] = (
    SCOPE_EVENTS_READ,
    SCOPE_STREAMS_READ,
    SCOPE_STREAMS_WRITE,
    SCOPE_SCHEMAS_READ,
    SCOPE_ANSWER_KEY_READ,
)
# Scopes a non-admin member may self-grant on a key (answer_key:read is
# admin-only, domain-model §2.2 / api-spec A-4).
ADMIN_ONLY_SCOPES: frozenset[str] = frozenset({SCOPE_ANSWER_KEY_READ})

_SLUG_RE = r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$"
_slug_validator = RegexValidator(regex=_SLUG_RE, message="Invalid workspace slug.")


class Workspace(models.Model):
    """The tenant (database-schema §3.3; domain-model §2.2 aggregate root).

    Self-tenant-owned: ``id`` is the tenant id (§9.4). Soft-delete tombstone
    ``deleted_at`` (C-9; INV-TEN-6 cascade is app-orchestrated).
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.TextField()
    slug = models.TextField(validators=[_slug_validator])
    plan = models.TextField(choices=PLAN_CHOICES, default=PLAN_FREE)
    created_by = models.ForeignKey(
        "identity.User", on_delete=models.RESTRICT, db_column="created_by", related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "workspaces"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(name__length__gte=1) & Q(name__length__lte=100),
                name="workspaces_name_len_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(slug__regex=_SLUG_RE), name="workspaces_slug_ck"
            ),
            models.CheckConstraint(
                condition=models.Q(plan__in=("free", "classroom", "pro")),
                name="workspaces_plan_ck",
            ),
            models.UniqueConstraint(
                Lower("slug"),
                condition=models.Q(deleted_at__isnull=True),
                name="workspaces_slug_uq",
            ),
        ]

    def __str__(self) -> str:
        return self.slug

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class Membership(WorkspaceScopedModel):
    """(user, workspace, role) (database-schema §3.4; INV-TEN-2/3)."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.RESTRICT, db_column="workspace_id", related_name="memberships"
    )
    user = models.ForeignKey(
        "identity.User", on_delete=models.RESTRICT, db_column="user_id", related_name="memberships"
    )
    role = models.TextField(choices=ROLE_CHOICES)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "memberships"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(role__in=("admin", "member")), name="memberships_role_ck"
            ),
            # INV-TEN-2: at most one membership per (user, workspace).
            models.UniqueConstraint(
                fields=["user", "workspace"], name="memberships_user_ws_uq"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace", "role"], name="memberships_ws_role_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.workspace_id}:{self.role}"


class WorkspaceInvitation(WorkspaceScopedModel):
    """Classroom onboarding invitation (database-schema §3.5).

    Reserved for the Phase 7 console invitation surface (api-spec §4.3 "refined
    in Phase 7"); the model + open-invite unique index land now so the schema is
    additive. 7-day expiry set at issuance.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)  # UUIDv7
    workspace = models.ForeignKey(
        Workspace, on_delete=models.RESTRICT, db_column="workspace_id", related_name="invitations"
    )
    email = models.TextField()  # normalized lowercase
    role = models.TextField(choices=ROLE_CHOICES, default=ROLE_MEMBER)
    token_hash = models.TextField(unique=True)
    invited_by = models.ForeignKey(
        "identity.User", on_delete=models.RESTRICT, db_column="invited_by", related_name="+"
    )
    expires_at = models.DateTimeField()  # issuance sets now() + 7 days
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        db_table = "workspace_invitations"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(role__in=("admin", "member")), name="ws_invites_role_ck"
            ),
            # One open invitation per address per workspace.
            models.UniqueConstraint(
                "workspace",
                Lower("email"),
                condition=models.Q(accepted_at__isnull=True),
                name="ws_invites_open_uq",
            ),
        ]

    def __str__(self) -> str:
        return f"invite:{self.email}@{self.workspace_id}"


_LAST4_RE = r"^[A-Za-z0-9]{4}$"


class ApiKey(WorkspaceScopedModel):
    """Data-plane credential (database-schema §3.6; ADR-0011; INV-TEN-4).

    Storage is SHA-256 hash + ``key_prefix`` + ``last4`` only — the plaintext
    secret lives solely in the creation response (SEC-KEY-3/4). Derived state
    (no stored status column): ``active`` ⇔ ``revoked_at IS NULL AND (expires_at
    IS NULL OR expires_at > now())``.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.RESTRICT, db_column="workspace_id", related_name="api_keys"
    )
    name = models.TextField()
    key_prefix = models.TextField(unique=True)  # e.g. 'df_live_a1b2c3d4'
    key_hash = models.TextField(unique=True)  # SHA-256 hex of the full key
    last4 = models.TextField()
    scopes = models.JSONField(default=list)  # text[] in PG; JSON list in the ORM
    created_by = models.ForeignKey(
        "identity.User", on_delete=models.RESTRICT, db_column="created_by", related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)  # null = non-expiring
    revoked_at = models.DateTimeField(null=True, blank=True)  # terminal
    revoked_by = models.ForeignKey(
        "identity.User",
        on_delete=models.RESTRICT,
        db_column="revoked_by",
        related_name="+",
        null=True,
        blank=True,
    )
    last_used_at = models.DateTimeField(null=True, blank=True)  # write-behind

    class Meta:
        db_table = "api_keys"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(name__length__gte=1) & Q(name__length__lte=100),
                name="api_keys_name_len_ck",
            ),
            models.CheckConstraint(
                condition=models.Q(last4__regex=_LAST4_RE), name="api_keys_last4_ck"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["workspace"], name="api_keys_ws_ix"),
        ]

    def __str__(self) -> str:
        return self.key_prefix

    def is_active(self, *, now: datetime | None = None) -> bool:
        """Derived ``active`` state (database-schema §3.6; SEC-KEY-8)."""
        moment = now or timezone.now()
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > moment

    @property
    def state(self) -> str:
        """``active | revoked | expired`` for the list serializer (api-spec §4.5)."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return "expired"
        return "active"


# Free-tier quota defaults (database-schema §3.7; PRD §7). Seeded into every new
# workspace in the creation transaction. Enforcement metering is Phase 11.
FREE_QUOTA_DEFAULTS: dict[str, int] = {
    "max_members": 3,
    "max_concurrent_streams": 2,
    "per_stream_tps_cap": 50,
    "aggregate_tps_cap": 100,
    "events_per_day": 1_000_000,
    "buffer_retention_hours": 24,
    "backfill_max_days": 7,
    "backfill_max_events": 1_000_000,
    "idle_pause_minutes": 120,
    "max_api_keys": 5,
}


class WorkspaceQuotas(WorkspaceScopedModel):
    """Plan-tier limits (database-schema §3.7; PRD §7).

    One row per workspace, created in the workspace-creation transaction with the
    Free-tier defaults above. The PK *is* ``workspace_id`` (no separate id), so
    the inherited ``WorkspaceScopedModel.workspace_id`` is promoted to the
    primary key here.
    """

    workspace = models.OneToOneField(
        Workspace,
        on_delete=models.RESTRICT,
        db_column="workspace_id",
        related_name="quotas",
        primary_key=True,
    )
    max_members = models.IntegerField(default=FREE_QUOTA_DEFAULTS["max_members"])
    max_concurrent_streams = models.IntegerField(
        default=FREE_QUOTA_DEFAULTS["max_concurrent_streams"]
    )
    per_stream_tps_cap = models.IntegerField(default=FREE_QUOTA_DEFAULTS["per_stream_tps_cap"])
    aggregate_tps_cap = models.IntegerField(default=FREE_QUOTA_DEFAULTS["aggregate_tps_cap"])
    events_per_day = models.BigIntegerField(default=FREE_QUOTA_DEFAULTS["events_per_day"])
    buffer_retention_hours = models.IntegerField(
        default=FREE_QUOTA_DEFAULTS["buffer_retention_hours"]
    )
    backfill_max_days = models.IntegerField(default=FREE_QUOTA_DEFAULTS["backfill_max_days"])
    backfill_max_events = models.BigIntegerField(
        default=FREE_QUOTA_DEFAULTS["backfill_max_events"]
    )
    idle_pause_minutes = models.IntegerField(default=FREE_QUOTA_DEFAULTS["idle_pause_minutes"])
    max_api_keys = models.IntegerField(default=FREE_QUOTA_DEFAULTS["max_api_keys"])
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspace_quotas"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(buffer_retention_hours__in=(24, 48)),
                name="ws_quotas_retention_ck",
            ),
        ]

    def __str__(self) -> str:
        return f"quotas:{self.workspace_id}"


class UsageCounter(WorkspaceScopedModel):
    """Events/day metering durable copy (database-schema §3.8).

    Model only this phase — runners increment Redis and a Celery flush upserts
    here; the quota check reads Redis falling back to this table. Metering wiring
    is Phase 11.
    """

    workspace = models.ForeignKey(
        Workspace, on_delete=models.RESTRICT, db_column="workspace_id", related_name="usage"
    )
    window_date = models.DateField()  # UTC day
    events_generated = models.BigIntegerField(default=0)
    events_delivered = models.BigIntegerField(default=0)
    backfill_events = models.BigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "usage_counters"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["workspace", "window_date"], name="usage_counters_pk"
            ),
        ]

    def __str__(self) -> str:
        return f"usage:{self.workspace_id}:{self.window_date}"
