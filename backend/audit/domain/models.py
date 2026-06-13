"""Domain models for the Audit context.

The single table is ``audit_log`` — the append-only security record
(database-schema §7.1, INV-AUD-1..4). Every concrete model sets ``Meta.db_table``
explicitly to the name fixed by specs/03-domain/database-schema.md (rule BE-APP-1,
C-2).

Append-only by construction: the model exposes no update/delete code path
(INV-AUD-1). The application writer (`audit.application.writer.record_audit`) is
the *only* INSERT site; reads go through `audit.application.reader`. There is no
viewset, serializer, or admin action that mutates a row — the table is
``SELECT, INSERT`` only even at the DB grant level (database-schema §9.2,
security §10.2).

Partitioning note: the production schema declares ``audit_log`` as a RANGE
(monthly) partitioned table on ``occurred_at`` (database-schema §7.1, §8.1). The
in-house partition manager (database-schema §8.2) is a later phase (Phase 11
backup/retention jobs); for Phase 2 this is a single physical table whose
*columns, constraints, and indexes match §7.1 exactly* — the partition machinery
attaches later without a schema reshape (M-1 additive). The composite primary key
``(audit_id, occurred_at)`` is carried now so the eventual partition conversion is
metadata-only.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import models
from django.utils import timezone

from audit.domain.ids import uuid7

# Actor-type discriminator (database-schema §7.1 ``audit_actor_ck``).
ACTOR_USER = "user"
ACTOR_API_KEY = "api_key"
ACTOR_SYSTEM = "system"
ACTOR_TYPES: tuple[str, str, str] = (ACTOR_USER, ACTOR_API_KEY, ACTOR_SYSTEM)
_ACTOR_CHOICES: list[tuple[str, str]] = [
    (ACTOR_USER, "User"),
    (ACTOR_API_KEY, "API key"),
    (ACTOR_SYSTEM, "System"),
]


class AuditLog(models.Model):
    """One immutable record of a security-relevant action (database-schema §7.1).

    ``action`` follows the ``{context}.{object}.{verb}`` convention (domain-model
    §2.10). ``workspace_id`` is NULL for account-level entries (INV-AUD-4); those
    are excluded from the workspace audit-log read surface (§10.4). ``metadata``
    and ``target`` never carry secret material (INV-AUD-3) — the writer strips
    secret-shaped keys defensively.
    """

    pk = models.CompositePrimaryKey("audit_id", "occurred_at")
    audit_id = models.UUIDField(default=uuid7, editable=False)  # UUIDv7 (C-3)
    # Wall clock + partition key (database-schema §7.1, §8.1). DB default now()
    # mirrors the DDL; the writer also stamps it so the value exists pre-commit.
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)
    workspace_id = models.UUIDField(null=True, blank=True)  # NULL ⇒ account-level (INV-AUD-4)
    actor_type = models.TextField(choices=_ACTOR_CHOICES)
    actor_user_id = models.UUIDField(null=True, blank=True)
    actor_api_key_id = models.UUIDField(null=True, blank=True)
    action = models.TextField()  # '{context}.{object}.{verb}' (domain-model §2.10)
    target_type = models.TextField()
    target_id = models.TextField()
    metadata = models.JSONField(default=dict)  # never secrets (INV-AUD-3)
    request_id = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "audit_log"  # C-2: fixed by database-schema §7.1
        # No ``ordering`` default: reads are explicitly ``-occurred_at`` (R-6).
        constraints: ClassVar[list[models.BaseConstraint]] = [
            # database-schema §7.1 ``audit_actor_ck``.
            models.CheckConstraint(
                condition=models.Q(actor_type__in=ACTOR_TYPES),
                name="audit_actor_ck",
            ),
            # database-schema §7.1 ``audit_actor_presence_ck``: the actor id field
            # matching ``actor_type`` is present (system actors carry neither id).
            models.CheckConstraint(
                condition=(
                    models.Q(actor_type=ACTOR_USER, actor_user_id__isnull=False)
                    | models.Q(actor_type=ACTOR_API_KEY, actor_api_key_id__isnull=False)
                    | models.Q(actor_type=ACTOR_SYSTEM)
                ),
                name="audit_actor_presence_ck",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            # Per-partition index templates (database-schema §7.1).
            models.Index(fields=["workspace_id", "-occurred_at"], name="audit_log_ws_ix"),
            models.Index(fields=["actor_user_id", "-occurred_at"], name="audit_log_user_ix"),
        ]

    def __str__(self) -> str:
        return f"{self.action}:{self.audit_id}"
