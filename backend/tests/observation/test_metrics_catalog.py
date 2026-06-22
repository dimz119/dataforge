"""Metrics catalog + M-3 cardinality-guard tests (phase-11 exit #8; observability §4).

The ``df_`` metrics catalog is the single source the SLO recording rules + alert
catalog name exact series against, and the M-3 cardinality rule (no
``workspace_id``/``stream_id``/``user_id``/``event_id`` labels) is BINDING. Gated here:

* the declared ``df_`` families are all present in the registry / on ``/metrics``
  (one representative per process group — a deleted metric would break a named SLI);
* the live registry passes ``assert_registry_cardinality`` (M-3 holds);
* no ``df_`` metric declares a banned label — re-derived by walking the registry, so
  a metric added without the ``_counter``/``_gauge``/``_histogram`` helpers is caught;
* histogram bucket sets follow M-5;
* the construction guard rejects a banned label (the enforcement, not just the state).

These cover the metrics half of exit #8 ("frozen log schema + metrics live, M-3 holds").
"""

from __future__ import annotations

import pytest

from observation.infra import metrics

# Representative metric names per process group (observability §4). If a named SLI's
# source series is deleted, this list catches it before the recording rules go inert.
_EXPECTED_METRIC_NAMES = frozenset(
    {
        # web
        "df_http_requests_total", "df_http_request_duration_seconds",
        "df_http_requests_in_flight", "df_auth_failures_total", "df_rate_limited_total",
        "df_cursor_expired_total", "df_events_served_total",
        # celery
        "df_celery_tasks_total", "df_celery_task_duration_seconds",
        "df_celery_queue_depth", "df_beat_last_run_timestamp_seconds",
        # runner
        "df_runner_active_leases", "df_runner_streams_running",
        "df_runner_lease_takeovers_total", "df_runner_tick_duration_seconds",
        "df_runner_tick_overruns_total", "df_generation_events_total",
        "df_ledger_append_duration_seconds", "df_ledger_append_failures_total",
        "df_checkpoint_duration_seconds", "df_checkpoint_age_seconds",
        "df_pool_entities", "df_quota_pauses_total",
        # kafka
        "df_kafka_publish_total", "df_kafka_publish_duration_seconds",
        "df_kafka_publish_retries_total", "df_kafka_consumer_lag",
        "df_kafka_consumer_fetch_total", "df_kafka_broker_up",
        # chaos
        "df_chaos_injections_total", "df_chaos_streams_enabled",
        "df_chaos_late_buffer_pending", "df_chaos_late_buffer_overdue",
        "df_chaos_reemissions_total",
        # buffer
        "df_buffer_writes_total", "df_buffer_write_batch_size",
        "df_buffer_commit_lag_seconds", "df_buffer_oldest_partition_age_seconds",
        "df_buffer_partitions_dropped_total", "df_buffer_rows",
        # ws
        "df_ws_connect_total", "df_ws_connections_active", "df_ws_frames_sent_total",
        "df_ws_frames_dropped_total", "df_ws_fanout_lag_seconds",
        "df_ws_connection_duration_seconds",
    }
)


def _registry_metric_names() -> set[str]:
    """Every metric family name in the live registry, with the ``_total`` family alias.

    ``CollectorRegistry.collect`` reports a counter's family name without the
    ``_total`` suffix (``df_http_requests`` for ``df_http_requests_total``); add the
    ``_total`` alias so the expected-name set (which uses the declared, suffixed names)
    matches whether the metric is a counter or not.
    """
    names: set[str] = set()
    for metric in metrics.REGISTRY.collect():
        names.add(metric.name)
        if metric.type == "counter":
            names.add(f"{metric.name}_total")
    return names


def test_full_df_catalog_is_present() -> None:
    """Every declared ``df_`` family is in the registry (no SLI source went missing)."""
    present = _registry_metric_names()
    missing = sorted(_EXPECTED_METRIC_NAMES - present)
    assert not missing, f"declared df_ metrics missing from the registry: {missing}"


def test_metrics_render_exposes_the_catalog() -> None:
    """``render_latest`` emits the catalog in the Prometheus text exposition format."""
    body, content_type = metrics.render_latest()
    text = body.decode("utf-8")
    assert "text/plain" in content_type  # CONTENT_TYPE_LATEST
    # A counter touched here so it appears as a sample; a gauge is always present via HELP.
    metrics.rate_limited_total.labels(scope="data-events").inc()
    body2, _ = metrics.render_latest()
    text2 = body2.decode("utf-8")
    assert "df_rate_limited_total" in text2
    assert 'scope="data-events"' in text2
    # HELP lines confirm every family is exported (representative spot checks).
    for name in ("df_http_requests_total", "df_runner_active_leases", "df_ws_connect_total"):
        assert f"# HELP {name}" in text or f"# HELP {name}" in text2, f"{name} not exported"


def test_registry_passes_m3_cardinality_guard() -> None:
    """The live registry passes ``assert_registry_cardinality`` (M-3 holds, exit #8)."""
    metrics.assert_registry_cardinality()  # raises CardinalityError on a banned label


def test_no_df_metric_declares_a_banned_label() -> None:
    """Walk the registry: no ``df_`` series carries a banned high-cardinality label (M-3).

    Independent of the construction guard — catches a metric registered outside the
    ``_counter``/``_gauge``/``_histogram`` helpers (the M-3 CI guard the build asks for)."""
    offenders: list[str] = []
    for metric in metrics.REGISTRY.collect():
        for sample in metric.samples:
            banned = set(sample.labels) & metrics.BANNED_LABELS
            if banned:
                offenders.append(f"{metric.name}{sorted(banned)}")
    assert not offenders, f"df_ metrics with banned labels (M-3): {offenders}"


def test_construction_guard_rejects_a_banned_label() -> None:
    """Declaring a metric with a banned label raises ``CardinalityError`` (the guard works)."""
    with pytest.raises(metrics.CardinalityError, match="workspace_id"):
        metrics._counter("df_test_banned_total", "should fail", ("workspace_id",))
    with pytest.raises(metrics.CardinalityError, match="stream_id"):
        metrics._gauge("df_test_banned_gauge", "should fail", ("stream_id",))


def test_histogram_buckets_follow_m5() -> None:
    """Histogram bucket sets match the M-5 contract (observability §4)."""
    # A labelled histogram emits no _bucket samples until a label-set is observed;
    # observe one value on each so the bucket boundaries are materialised.
    metrics.http_request_duration_seconds.labels(method="GET", route="/x").observe(0.01)
    metrics.runner_tick_duration_seconds.observe(0.01)
    metrics.buffer_commit_lag_seconds.observe(1.0)

    def bucket_uppers(name: str) -> list[float]:
        uppers: list[float] = []
        for metric in metrics.REGISTRY.collect():
            if metric.name == name:
                for sample in metric.samples:
                    if sample.name.endswith("_bucket"):
                        le = sample.labels["le"]
                        if le != "+Inf":
                            uppers.append(float(le))
        return sorted(set(uppers))

    # http + sink_commit family.
    assert bucket_uppers("df_http_request_duration_seconds") == [
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0
    ]
    # tick / publish / append inner-loop family.
    assert bucket_uppers("df_runner_tick_duration_seconds") == [
        0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5
    ]
    # lag family.
    assert bucket_uppers("df_buffer_commit_lag_seconds") == [
        0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 1800.0
    ]
