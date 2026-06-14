"""``manage.py set_workspace_quota`` — ops/demo plan-tier quota override.

Raises (or lowers) a workspace's :class:`WorkspaceQuotas` caps. The Free-tier
defaults (per_stream_tps_cap=50, aggregate_tps_cap=100, …) are deliberately small
(database-schema §3.7; PRD §7), so exercising the spec'd high-throughput paths —
the OPS-5 dynamic-TPS stopwatch (10→500) and SOAK-200 (200 TPS) — requires a plan
upgrade. This is the plan-management operation that does that; it is admin/ops
tooling, never a tenant-facing API (quota self-service is out of MVP scope).

Runs as the owner (``dataforge_migrate``) role: the runtime NOBYPASSRLS role holds
only ``SELECT, INSERT`` on ``workspace_quotas`` (the §3.7 grant policy
provision_db_roles encodes), so an UPDATE needs the owner connection — the same
``maintenance`` alias the partition-maintenance tasks use. On non-Postgres / when
the owner alias is absent it falls back to the default connection (the unit lane
connects as the owner via ``default``).

Idempotent; only the fields named on the command line are changed.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# The mutable integer quota fields this command may override (a conservative
# allowlist so a typo can never write an arbitrary attribute).
_FIELDS: tuple[str, ...] = (
    "max_members",
    "max_concurrent_streams",
    "per_stream_tps_cap",
    "aggregate_tps_cap",
    "events_per_day",
    "backfill_max_days",
    "backfill_max_events",
    "idle_pause_minutes",
    "max_api_keys",
)

_MAINTENANCE_ALIAS = "maintenance"


class Command(BaseCommand):
    help = "Override a workspace's plan-tier quota caps (ops/demo tooling)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("workspace_id", help="The target workspace UUID.")
        for field in _FIELDS:
            parser.add_argument(
                f"--{field.replace('_', '-')}",
                type=int,
                default=None,
                dest=field,
                help=f"New value for {field}.",
            )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            ws_id = uuid.UUID(str(options["workspace_id"]))
        except (ValueError, AttributeError, TypeError) as exc:
            raise CommandError("workspace_id must be a UUID") from exc

        updates = {f: options[f] for f in _FIELDS if options.get(f) is not None}
        if not updates:
            raise CommandError(
                "Pass at least one quota field, e.g. --per-stream-tps-cap 1000"
            )
        for field, value in updates.items():
            if value < 0:
                raise CommandError(f"{field} must be >= 0 (got {value})")

        alias = _MAINTENANCE_ALIAS if _MAINTENANCE_ALIAS in connections else "default"
        conn = connections[alias]
        # Class P (platform-managed) row: the owner role updates it directly; no
        # workspace GUC arming is needed because the owner is not subject to the
        # tenant RLS policy for this maintenance write (security §3.7). The
        # ``maintenance`` alias is autocommit, so the UPDATE commits on cursor exit;
        # an atomic() guard keeps the fallback ``default`` (unit-lane) write durable
        # too without depending on autocommit mode.
        from django.db import transaction

        with transaction.atomic(using=alias), conn.cursor() as cursor:
            assignments = ", ".join(f"{f} = %s" for f in updates)
            params = [*updates.values(), str(ws_id)]
            cursor.execute(
                f"UPDATE workspace_quotas SET {assignments}, updated_at = now() "
                f"WHERE workspace_id = %s",
                params,
            )
            changed = cursor.rowcount
        if changed == 0:
            raise CommandError(f"No workspace_quotas row for workspace {ws_id}")
        self.stdout.write(
            self.style.SUCCESS(f"set_workspace_quota: updated {ws_id} {updates}")
        )
