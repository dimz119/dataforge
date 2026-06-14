"""``manage.py drop_ledger_partitions`` — manual ledger-partition retention drop.

The interim manual drop for the ground-truth ledger's 7-day rolling retention
(database-schema §8.3: partition drop, 7 days). Phase 5's hourly
``manage_partitions`` beat task automates this and Phase 11 adds pre-drop
export-to-object-storage; until then this command detach-then-drops every daily
partition whose entire range is older than ``--retention-days`` (default 7).

Detach-then-drop keeps the parent lockless for readers (§8.2 step 2). ``--dry-run``
lists what would be dropped without dropping. DDL runs as the table owner (the
DDL-class command set repoints ``default`` to the migrate role).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection

from generation.infra import partitions


class Command(BaseCommand):
    help = "Detach-then-drop ground_truth_ledger partitions older than the retention horizon."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--retention-days",
            type=int,
            default=7,
            help="Drop partitions whose day is older than this many days (default 7, §8.3).",
        )
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=60,
            help="How far back to scan for droppable partitions (default 60).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List partitions that would be dropped without dropping them.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if connection.vendor != "postgresql":
            self.stdout.write("Skipping: partitioning is a PostgreSQL construct.")
            return
        retention = int(options["retention_days"])
        lookback = int(options["lookback_days"])
        dry_run = bool(options["dry_run"])
        cutoff = datetime.now(UTC).date() - timedelta(days=retention)
        existing = self._existing_partitions()
        dropped = 0
        for day in partitions.daily_range(cutoff - timedelta(days=lookback), lookback):
            name = partitions.partition_name(day)
            if name not in existing:
                continue
            if dry_run:
                self.stdout.write(f"would drop {name}")
                continue
            with connection.cursor() as cursor:
                partitions.drop_partition(cursor, day)
            self.stdout.write(self.style.SUCCESS(f"dropped {name}"))
            dropped += 1
        verb = "would drop" if dry_run else "dropped"
        self.stdout.write(self.style.SUCCESS(f"{verb} {dropped} ledger partition(s)."))

    def _existing_partitions(self) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT inhrelid::regclass::text FROM pg_inherits "
                "WHERE inhparent = %s::regclass",
                [partitions.LEDGER_TABLE],
            )
            return {row[0] for row in cursor.fetchall()}
