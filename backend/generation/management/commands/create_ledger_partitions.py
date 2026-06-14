"""``manage.py create_ledger_partitions`` — pre-create daily ledger partitions.

The interim partition-manager surface for the ground-truth ledger
(database-schema §8.1-8.2; M-5: individual partitions are owned by the partition
manager, never migrations). Phase 5 wires the hourly ``manage_partitions`` Celery
beat task; until then this command create-and-attaches today + ``--days-ahead``
daily partitions (default 3, the §8.1 "Pre-created ahead" for the ledger), each
with the §5.5 index template and §9.7 RLS template. Idempotent (``IF NOT
EXISTS``), so it is safe to re-run.

DDL runs as the table owner; it is in the DDL-class command set so the ``default``
connection is repointed to the migrate (owner) role for the run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection

from generation.infra import partitions


class Command(BaseCommand):
    help = "Create-and-attach daily ground_truth_ledger partitions (today + N ahead)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--days-ahead",
            type=int,
            default=3,
            help="Number of future daily partitions to create (default 3, §8.1).",
        )
        parser.add_argument(
            "--start",
            type=str,
            default="",
            help="Start day YYYY-MM-DD (default: today, UTC).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if connection.vendor != "postgresql":
            self.stdout.write("Skipping: partitioning is a PostgreSQL construct.")
            return
        start = self._start_day(options["start"])
        days_ahead = int(options["days_ahead"])
        with connection.cursor() as cursor:
            names = partitions.ensure_partitions(cursor, start=start, days_ahead=days_ahead)
        for name in names:
            self.stdout.write(self.style.SUCCESS(f"ensured partition {name}"))
        self.stdout.write(self.style.SUCCESS(f"{len(names)} ledger partition(s) ensured."))

    def _start_day(self, raw: str) -> date:
        if raw:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).date()
        return datetime.now(UTC).date()
