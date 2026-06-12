"""`manage.py provision_kafka_topics` — idempotent internal topic provisioning.

Dev runs it from the `api` entrypoint; prod runs it in the Fly release command
(deployment-architecture §2.2, §3.2). Auto-creation is off everywhere, so this
command is the only way topics come to exist.
"""

from typing import Any

from confluent_kafka.admin import AdminClient
from django.conf import settings
from django.core.management.base import BaseCommand

from delivery.infra.kafka_topics import INTERNAL_TOPICS, ensure_topics


class Command(BaseCommand):
    help = (
        "Idempotently create the internal Kafka topics "
        "(df.delivery.events.v1, 12 partitions — backend-architecture §9.1)."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        admin = AdminClient({"bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS})
        created = ensure_topics(admin)
        for spec in INTERNAL_TOPICS:
            state = "created" if spec.name in created else "exists"
            self.stdout.write(
                f"{spec.name}: {state} ({spec.partitions} partitions, "
                f"replication {spec.replication_factor})"
            )
