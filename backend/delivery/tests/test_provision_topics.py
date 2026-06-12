"""Unit tests for idempotent Kafka topic provisioning — no live broker."""

from typing import Any

from confluent_kafka import KafkaError, KafkaException

from delivery.infra.kafka_topics import DELIVERY_EVENTS_V1, ensure_topics


class FakeMetadata:
    def __init__(self, topics: dict[str, object]) -> None:
        self.topics = topics


class FakeFuture:
    def __init__(self, error: KafkaException | None = None) -> None:
        self._error = error

    def result(self, timeout: float | None = None) -> None:
        if self._error is not None:
            raise self._error


class FakeAdmin:
    def __init__(
        self,
        existing: dict[str, object] | None = None,
        create_error: KafkaException | None = None,
    ) -> None:
        self._existing = existing or {}
        self._create_error = create_error
        self.created_requests: list[Any] = []

    def list_topics(self, timeout: float | None = None) -> FakeMetadata:
        return FakeMetadata(self._existing)

    def create_topics(
        self, new_topics: list[Any], request_timeout: float | None = None
    ) -> dict[str, FakeFuture]:
        self.created_requests.extend(new_topics)
        return {topic.topic: FakeFuture(self._create_error) for topic in new_topics}


def test_topic_contract_matches_backend_architecture_9_1() -> None:
    assert DELIVERY_EVENTS_V1.name == "df.delivery.events.v1"
    assert DELIVERY_EVENTS_V1.partitions == 12
    assert DELIVERY_EVENTS_V1.replication_factor == 1
    assert DELIVERY_EVENTS_V1.config["retention.ms"] == "21600000"
    assert DELIVERY_EVENTS_V1.config["retention.bytes"] == "5368709120"
    assert DELIVERY_EVENTS_V1.config["max.message.bytes"] == "262144"


def test_creates_missing_topic() -> None:
    admin = FakeAdmin(existing={})
    created = ensure_topics(admin)  # type: ignore[arg-type]
    assert created == ["df.delivery.events.v1"]
    assert admin.created_requests[0].topic == "df.delivery.events.v1"


def test_skips_existing_topic() -> None:
    admin = FakeAdmin(existing={"df.delivery.events.v1": object()})
    assert ensure_topics(admin) == []  # type: ignore[arg-type]
    assert admin.created_requests == []


def test_create_race_topic_already_exists_is_success() -> None:
    error = KafkaException(KafkaError(KafkaError.TOPIC_ALREADY_EXISTS))
    admin = FakeAdmin(existing={}, create_error=error)
    assert ensure_topics(admin) == []  # type: ignore[arg-type]


def test_other_create_failure_raises() -> None:
    error = KafkaException(KafkaError(KafkaError.NETWORK_EXCEPTION))
    admin = FakeAdmin(existing={}, create_error=error)
    try:
        ensure_topics(admin)  # type: ignore[arg-type]
    except KafkaException:
        pass
    else:
        raise AssertionError("expected KafkaException to propagate")
