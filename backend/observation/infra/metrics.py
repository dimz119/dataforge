"""The DataForge metrics catalog — the single registry of every ``df_`` metric
(observability §4).

Every process group imports the metric *objects* declared here and calls
``.inc()`` / ``.observe()`` / ``.set()`` directly; no instrumentation code
declares its own metric. Centralising the catalog is what lets the SLO recording
rules and the alert catalog name exact series (observability §7, §9).

**Cardinality (M-3, BINDING).** ``workspace_id`` / ``stream_id`` / ``user_id`` /
``event_id`` are forbidden as metric labels — they are unbounded and would blow up
the series count. They live in *logs* (the frozen field schema), correlated by
``request_id``, not in metric labels. ``_assert_label_cardinality`` enforces this at
construction time, and the CI test re-checks the whole registry.

The registry is process-local (a fresh ``CollectorRegistry``), exposed on
``DF_METRICS_PORT`` (default 9091) by ``start_metrics_server`` /
``metrics_wsgi_app``. Histogram buckets follow M-5 exactly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --- M-3 cardinality guard ---------------------------------------------------
# These identifiers are unbounded per-tenant/per-entity and are BANNED as labels
# on any df_ metric (observability §4, M-3). They belong in logs only.
BANNED_LABELS = frozenset({"workspace_id", "stream_id", "user_id", "event_id"})


class CardinalityError(AssertionError):
    """Raised when a df_ metric declares a banned high-cardinality label (M-3)."""


def _assert_label_cardinality(name: str, labels: Iterable[str]) -> tuple[str, ...]:
    """Reject any banned label at construction time (M-3 enforcement)."""
    label_tuple = tuple(labels)
    offending = sorted(set(label_tuple) & BANNED_LABELS)
    if offending:
        raise CardinalityError(
            f"metric {name!r} declares banned high-cardinality label(s) "
            f"{offending} — M-3 forbids workspace_id/stream_id/user_id/event_id "
            f"as metric labels (observability §4). Put them in logs instead."
        )
    return label_tuple


# Process-local registry: one per process, scraped on DF_METRICS_PORT.
REGISTRY = CollectorRegistry()


# --- M-5 histogram bucket sets ----------------------------------------------
# http + sink_commit family.
_BUCKETS_REQUEST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
# tick / publish / append family (fast inner-loop latencies).
_BUCKETS_INNER = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)
# lag family (seconds, long tail).
_BUCKETS_LAG = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 1800.0)
# buffer write batch size (rows, not seconds).
_BUCKETS_BATCH_SIZE = (1, 10, 50, 100, 500, 1000, 5000)
# ws connection duration (seconds, very long tail).
_BUCKETS_WS_DURATION = (1, 10, 60, 300, 1800, 7200)


def _counter(name: str, doc: str, labels: tuple[str, ...] = ()) -> Counter:
    return Counter(name, doc, _assert_label_cardinality(name, labels), registry=REGISTRY)


def _gauge(name: str, doc: str, labels: tuple[str, ...] = ()) -> Gauge:
    return Gauge(name, doc, _assert_label_cardinality(name, labels), registry=REGISTRY)


def _histogram(
    name: str, doc: str, buckets: tuple[float, ...], labels: tuple[str, ...] = ()
) -> Histogram:
    return Histogram(
        name, doc, _assert_label_cardinality(name, labels), buckets=buckets, registry=REGISTRY
    )


# ===========================================================================
# web (observability §4)
# ===========================================================================
http_requests_total = _counter(
    "df_http_requests_total",
    "HTTP requests handled by the web tier.",
    ("method", "route", "status"),
)
http_request_duration_seconds = _histogram(
    "df_http_request_duration_seconds",
    "HTTP request wall-clock latency.",
    _BUCKETS_REQUEST,
    ("method", "route"),
)
http_requests_in_flight = _gauge(
    "df_http_requests_in_flight", "HTTP requests currently being served."
)
auth_failures_total = _counter(
    "df_auth_failures_total", "Authentication rejections.", ("mechanism", "reason")
)
rate_limited_total = _counter(
    "df_rate_limited_total", "Requests rejected by per-key rate limits.", ("scope",)
)
cursor_expired_total = _counter(
    "df_cursor_expired_total", "REST cursor pagination tokens rejected as expired."
)
events_served_total = _counter(
    "df_events_served_total", "Events served to consumers.", ("channel",)
)

# ===========================================================================
# celery (observability §4)
# ===========================================================================
celery_tasks_total = _counter(
    "df_celery_tasks_total", "Celery task terminal outcomes.", ("task", "state")
)
celery_task_duration_seconds = _histogram(
    "df_celery_task_duration_seconds", "Celery task execution time.", _BUCKETS_REQUEST, ("task",)
)
celery_queue_depth = _gauge(
    "df_celery_queue_depth", "Pending messages per Celery queue.", ("queue",)
)
beat_last_run_timestamp_seconds = _gauge(
    "df_beat_last_run_timestamp_seconds",
    "Unix timestamp of the last beat fire per schedule.",
    ("schedule",),
)

# ===========================================================================
# runner (observability §4)
# ===========================================================================
runner_active_leases = _gauge("df_runner_active_leases", "Shard leases held by this runner.")
runner_streams_running = _gauge("df_runner_streams_running", "Streams actively generating here.")
runner_lease_takeovers_total = _counter(
    "df_runner_lease_takeovers_total", "Shard lease takeovers.", ("reason",)
)
runner_tick_duration_seconds = _histogram(
    "df_runner_tick_duration_seconds", "Generation tick wall-clock time.", _BUCKETS_INNER
)
runner_tick_overruns_total = _counter(
    "df_runner_tick_overruns_total", "Ticks that ran past their scheduled interval."
)
generation_events_total = _counter(
    "df_generation_events_total", "Events generated.", ("event_class",)
)
ledger_append_duration_seconds = _histogram(
    "df_ledger_append_duration_seconds", "ground_truth_ledger append latency.", _BUCKETS_INNER
)
ledger_append_failures_total = _counter(
    "df_ledger_append_failures_total", "ground_truth_ledger append failures."
)
checkpoint_duration_seconds = _histogram(
    "df_checkpoint_duration_seconds", "Checkpoint persist latency.", _BUCKETS_INNER
)
checkpoint_age_seconds = _gauge(
    "df_checkpoint_age_seconds", "Age of the most recent committed checkpoint."
)
pool_entities = _gauge(
    "df_pool_entities", "Entities resident in the actor pool.", ("entity_class",)
)
quota_pauses_total = _counter(
    "df_quota_pauses_total", "Streams system-paused for quota/idle.", ("reason",)
)

# ===========================================================================
# kafka (observability §4)
# ===========================================================================
kafka_publish_total = _counter("df_kafka_publish_total", "Kafka produce attempts.", ("result",))
kafka_publish_duration_seconds = _histogram(
    "df_kafka_publish_duration_seconds", "Kafka produce latency.", _BUCKETS_INNER
)
kafka_publish_retries_total = _counter(
    "df_kafka_publish_retries_total", "Kafka produce retries."
)
kafka_consumer_lag = _gauge(
    "df_kafka_consumer_lag",
    "Consumer-group offset lag (messages).",
    ("group", "topic", "partition"),
)
kafka_consumer_fetch_total = _counter(
    "df_kafka_consumer_fetch_total", "Kafka consumer fetch batches.", ("group",)
)
kafka_broker_up = _gauge("df_kafka_broker_up", "1 if this process reaches the broker, else 0.")

# ===========================================================================
# chaos (observability §4)
# ===========================================================================
chaos_injections_total = _counter(
    "df_chaos_injections_total", "Chaos fault injections.", ("mode",)
)
chaos_streams_enabled = _gauge(
    "df_chaos_streams_enabled", "Streams with a chaos mode enabled.", ("mode",)
)
chaos_late_buffer_pending = _gauge(
    "df_chaos_late_buffer_pending", "Late-arrival buffer entries pending re-emission."
)
chaos_late_buffer_overdue = _gauge(
    "df_chaos_late_buffer_overdue", "Late-arrival buffer entries past their due_at."
)
chaos_reemissions_total = _counter(
    "df_chaos_reemissions_total", "Late-arrival buffer re-emissions.", ("outcome",)
)

# ===========================================================================
# buffer (observability §4)
# ===========================================================================
buffer_writes_total = _counter(
    "df_buffer_writes_total", "event_buffer write outcomes.", ("result",)
)
buffer_write_batch_size = _histogram(
    "df_buffer_write_batch_size", "Rows per buffer COPY batch.", _BUCKETS_BATCH_SIZE
)
buffer_commit_lag_seconds = _histogram(
    "df_buffer_commit_lag_seconds",
    "Seconds from canonical emitted_at to buffer visibility (SLO-2).",
    _BUCKETS_LAG,
)
buffer_oldest_partition_age_seconds = _gauge(
    "df_buffer_oldest_partition_age_seconds", "Age of the oldest retained buffer partition."
)
buffer_partitions_dropped_total = _counter(
    "df_buffer_partitions_dropped_total", "event_buffer partitions dropped by retention."
)
buffer_rows = _gauge("df_buffer_rows", "Rows currently resident in event_buffer.")

# ===========================================================================
# ws (observability §4)
# ===========================================================================
ws_connect_total = _counter(
    "df_ws_connect_total", "WebSocket connection attempts by outcome.", ("result",)
)
ws_connections_active = _gauge("df_ws_connections_active", "Live WebSocket connections.")
ws_frames_sent_total = _counter("df_ws_frames_sent_total", "WebSocket frames sent to clients.")
ws_frames_dropped_total = _counter(
    "df_ws_frames_dropped_total", "WebSocket frames dropped.", ("reason",)
)
ws_fanout_lag_seconds = _histogram(
    "df_ws_fanout_lag_seconds", "Seconds from emit to WS frame send.", _BUCKETS_LAG
)
ws_connection_duration_seconds = _histogram(
    "df_ws_connection_duration_seconds", "WebSocket connection lifetime.", _BUCKETS_WS_DURATION
)


# ===========================================================================
# Exposer (observability §4, §6.3)
# ===========================================================================
def render_latest() -> tuple[bytes, str]:
    """Return ``(body, content_type)`` for the current registry snapshot."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def assert_registry_cardinality() -> None:
    """Re-validate the live registry against M-3 (CI guard / defensive boot check).

    Construction already rejects banned labels, but this walks every collected
    series so a metric added without the ``_counter``/``_gauge``/``_histogram``
    helpers is still caught.
    """
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            offending = sorted(set(sample.labels) & BANNED_LABELS)
            if offending:
                raise CardinalityError(
                    f"metric {metric.name!r} emits banned label(s) {offending} (M-3)."
                )


def metrics_wsgi_app(
    environ: dict[str, object],
    start_response: Callable[[str, list[tuple[str, str]]], object],
) -> list[bytes]:
    """Minimal WSGI app exposing the registry at any path (mountable under /metrics).

    The ``web`` tier serves /metrics through Django (``observation.api.health``),
    but this standalone WSGI app lets a deployment mount the exposer on a side port
    for any WSGI process without Django routing. Process groups without a WSGI
    server use ``start_metrics_server`` instead.
    """
    body, content_type = render_latest()
    start_response(
        "200 OK",
        [("Content-Type", content_type), ("Content-Length", str(len(body)))],
    )
    return [body]


_server_started = False


def start_metrics_server(port: int) -> bool:
    """Start the background HTTP exposer for non-WSGI process groups.

    Used by celery (worker/beat), the runner, and the sink hosts (buffer-writer,
    ws-pusher) — each runs its own thread serving the process registry on ``port``.
    Idempotent and a no-op when ``port <= 0`` (tests disable the exposer). Returns
    ``True`` when a server was started.
    """
    global _server_started
    if port <= 0 or _server_started:
        return False
    from prometheus_client import start_http_server

    start_http_server(port, registry=REGISTRY)
    _server_started = True
    return True
