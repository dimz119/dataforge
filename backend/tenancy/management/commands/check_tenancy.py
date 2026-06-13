"""``manage.py check_tenancy`` — the Layer-1 CI guard (security §4.1).

Fails (exit non-zero, naming the offender) if any tenant model lacks a
``workspace_id`` column, uses the default manager instead of
``WorkspaceScopedManager``, lacks an RLS migration, or is exposed by a viewset
not extending ``ScopedModelViewSet``. The classification is *closed*: an
unclassified model (neither tenant-owned nor exempt) also fails.

This command runs on every PR forever (Phase 2 exit criterion #3 / testing §7.4)
and is the static half of the "a breach requires two simultaneous failures"
proof — a planted unscoped model makes the build red before it can ship.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from tenancy.infra.tenancy_check import run_checks


class Command(BaseCommand):
    help = (
        "Static tenancy guard: every model is tenant-owned (scoped manager + "
        "workspace_id + RLS migration) or explicitly exempt; every tenant viewset "
        "extends ScopedModelViewSet (security-architecture §4.1)."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        violations = run_checks()
        if violations:
            for v in violations:
                self.stderr.write(self.style.ERROR(f"  ✗ {v}"))
            raise CommandError(
                f"check_tenancy FAILED with {len(violations)} violation(s) — see above. "
                f"No tenant model may bypass the scoped manager / RLS / scoped viewset stack."
            )
        self.stdout.write(self.style.SUCCESS("check_tenancy PASSED — tenancy stack intact."))
