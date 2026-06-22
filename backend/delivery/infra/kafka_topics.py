"""Internal Kafka topic layout and idempotent provisioning (backend-architecture §9.1).

One topic in the MVP: `df.delivery.events.v1`, 12 partitions, delete cleanup,
retention 6 h AND 5 GiB per partition, 256 KiB max message — values copied
from the §9.1 topic table.

**Sharding & partitions (Phase 11, scaling-strategy §2.3/§3).** The internal topic is
**shared across all workspaces and shards** — there are zero per-workspace or per-shard
topics (topic-per-tenant would explode partition count on a single broker). A multi-shard
stream does NOT get dedicated partitions: every event is keyed by ``partition_key``
(``{workspace_id}:{stream_id}:{entity_type}:{entity_key}``), so each *actor's* events land
on one partition and stay ordered, and because actors are partitioned to disjoint shards by
``shard_for_key`` (no two shards share an actor) a shard's keyspace is naturally disjoint at
the broker. The partition COUNT is sized for aggregate fleet TPS, not shard count: 12 at GA
(≥ 64 shards' worth of keyspace fits behind the workspace aggregate-TPS quota, §2.3), 48 at
rung 5, 192 at rung 6. Partition growth happens **only via a new topic generation**
(``…events.v2`` + consumer cutover), NEVER in-place ``kafka-topics --alter`` — in-place
addition would remap key→partition mid-stream and break per-key ordering (§2.3). So this
spec is intentionally static at 12; bumping it is a v2 topic, not an edit here.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from confluent_kafka import KafkaError, KafkaException

# NewTopic is re-exported without __all__, hence the targeted ignore.
from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]


@dataclass(frozen=True)
class TopicSpec:
    name: str
    partitions: int
    replication_factor: int
    config: dict[str, str] = field(default_factory=dict)


DELIVERY_EVENTS_V1 = TopicSpec(
    name="df.delivery.events.v1",
    partitions=12,
    replication_factor=1,
    config={
        "cleanup.policy": "delete",
        "retention.ms": "21600000",  # 6 h (backend-architecture §9.1)
        "retention.bytes": "5368709120",  # 5 GiB per partition
        "max.message.bytes": "262144",  # 256 KiB
    },
)

INTERNAL_TOPICS: tuple[TopicSpec, ...] = (DELIVERY_EVENTS_V1,)


def ensure_topics(
    admin: AdminClient,
    topics: Sequence[TopicSpec] = INTERNAL_TOPICS,
    timeout: float = 10.0,
) -> list[str]:
    """Create any missing topics; never alter existing ones (deployment §3.4).

    Idempotent: topics already present are skipped, and a concurrent-create
    race (TOPIC_ALREADY_EXISTS) is treated as success. Returns the names
    actually created.
    """
    existing = set(admin.list_topics(timeout=timeout).topics)
    missing = [spec for spec in topics if spec.name not in existing]
    if not missing:
        return []

    futures = admin.create_topics(
        [
            NewTopic(
                spec.name,
                num_partitions=spec.partitions,
                replication_factor=spec.replication_factor,
                config=spec.config,
            )
            for spec in missing
        ],
        request_timeout=timeout,
    )

    created: list[str] = []
    for name, future in futures.items():
        try:
            future.result(timeout)
            created.append(name)
        except KafkaException as exc:
            error: KafkaError = exc.args[0]
            if error.code() == KafkaError.TOPIC_ALREADY_EXISTS:
                continue  # lost a benign create race — still provisioned
            raise
    return created
