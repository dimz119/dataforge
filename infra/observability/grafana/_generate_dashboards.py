#!/usr/bin/env python3
"""Generate the six DataForge Grafana dashboards (Phase 11, P11-06).

Panel lists are frozen in observability.md §11. One dashboard per PROCESS GROUP
plus the SLO-burn overview:

  slo-burn       <- "SLO overview" (§11): three SLI gauges + 30-day burn-down,
                    budget remaining, current burn rate.
  control-plane  <- "Control plane" (§11): request rate / error rate / p50-p99
                    by route; auth failures; rate limiting; Celery queues + beat.
  runner-generation <- "Data plane" (§11): aggregate generation TPS (business vs
                    CDC); tick duration + overruns; lease map; checkpoint age;
                    ledger append health.
  kafka-sinks    <- "Backbone & sinks" (§11): publish rate/failures; consumer
                    lag per group/partition; buffer commit-lag heatmap;
                    oldest-partition age. (WS panels split into the ws dashboard.)
  ws             <- "Backbone & sinks" WS panels (§11): WS connections / frames /
                    drops, fanout lag.
  chaos          <- "Chaos" (§11): injections by mode; late-buffer pending/overdue;
                    re-emission outcomes.

All queries use the exact df_ metric names + labels from
observation/infra/metrics.py and the SLI recording rules in
prometheus/slo-recording-rules.yml. Output is minimal-but-valid Grafana
dashboard JSON (schemaVersion 39, Grafana 10+). Datasource is a templated
${DS_PROMETHEUS} so provisioning binds it.

Run:  python infra/observability/grafana/_generate_dashboards.py
"""
from __future__ import annotations

import json
import pathlib

DS = "${DS_PROMETHEUS}"
DASH_DIR = pathlib.Path(__file__).parent / "dashboards"


def _datasource() -> dict:
    return {"type": "prometheus", "uid": DS}


def _target(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {
        "datasource": _datasource(),
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "editorMode": "code",
        "range": True,
    }


_NEXT_ID = {"v": 1}


def _new_id() -> int:
    i = _NEXT_ID["v"]
    _NEXT_ID["v"] += 1
    return i


def _grid(x: int, y: int, w: int, h: int) -> dict:
    return {"h": h, "w": w, "x": x, "y": y}


def timeseries(title: str, targets: list[dict], grid: dict, unit: str = "short") -> dict:
    return {
        "id": _new_id(),
        "type": "timeseries",
        "title": title,
        "datasource": _datasource(),
        "gridPos": grid,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"drawStyle": "line", "fillOpacity": 10, "lineWidth": 1},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title: str, targets: list[dict], grid: dict, unit: str = "short") -> dict:
    return {
        "id": _new_id(),
        "type": "stat",
        "title": title,
        "datasource": _datasource(),
        "gridPos": grid,
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def gauge(title: str, targets: list[dict], grid: dict, unit: str = "percentunit") -> dict:
    return {
        "id": _new_id(),
        "type": "gauge",
        "title": title,
        "datasource": _datasource(),
        "gridPos": grid,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0.9,
                "max": 1,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "orange", "value": 0.99},
                        {"color": "green", "value": 0.995},
                    ],
                },
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
    }


def heatmap(title: str, expr: str, grid: dict) -> dict:
    return {
        "id": _new_id(),
        "type": "heatmap",
        "title": title,
        "datasource": _datasource(),
        "gridPos": grid,
        "targets": [
            {
                "datasource": _datasource(),
                "expr": expr,
                "format": "heatmap",
                "refId": "A",
                "editorMode": "code",
                "range": True,
            }
        ],
        "options": {"calculate": False, "cellGap": 1, "yAxis": {"unit": "s"}},
    }


def dashboard(uid: str, title: str, tags: list[str], panels: list[dict]) -> dict:
    _NEXT_ID["v"] = 1  # reset per-dashboard ids
    return {
        "uid": uid,
        "title": title,
        "tags": ["dataforge", *tags],
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "timezone": "utc",
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "30s",
        "templating": {
            "list": [
                {
                    "name": "DS_PROMETHEUS",
                    "type": "datasource",
                    "query": "prometheus",
                    "label": "Prometheus",
                    "current": {},
                    "hide": 0,
                }
            ]
        },
        "panels": panels,
    }


def build_slo_burn() -> dict:
    # "SLO overview": three SLI gauges + 30-day burn-down + budget remaining + burn rate.
    panels = [
        gauge(
            "SLO-1 API availability (30d)",
            [_target("sli:slo1_api_availability:ratio_rate30d", "availability")],
            _grid(0, 0, 8, 8),
        ),
        gauge(
            "SLO-2 delivery success (30d)",
            [_target("sli:slo2_delivery_success:ratio_rate30d", "delivery")],
            _grid(8, 0, 8, 8),
        ),
        gauge(
            "SLO-3 WS connect (30d)",
            [_target("sli:slo3_ws_connect:ratio_rate30d", "ws connect")],
            _grid(16, 0, 8, 8),
        ),
        # Error-budget remaining = 1 - (errorRatio / errorBudget). 1 = full budget.
        timeseries(
            "Error budget remaining (30d, fraction)",
            [
                _target(
                    "1 - ((1 - sli:slo1_api_availability:ratio_rate30d) / (1 - 0.995))",
                    "SLO-1",
                    "A",
                ),
                _target(
                    "1 - ((1 - sli:slo2_delivery_success:ratio_rate30d) / (1 - 0.99))",
                    "SLO-2",
                    "B",
                ),
                _target(
                    "1 - ((1 - sli:slo3_ws_connect:ratio_rate30d) / (1 - 0.995))",
                    "SLO-3",
                    "C",
                ),
            ],
            _grid(0, 8, 12, 8),
            unit="percentunit",
        ),
        # Current fast-burn rate (1h window) per SLO, in budget-multiples.
        timeseries(
            "Current burn rate (1h, x budget)",
            [
                _target(
                    "(1 - sli:slo1_api_availability:ratio_rate1h) / (1 - 0.995)",
                    "SLO-1",
                    "A",
                ),
                _target(
                    "(1 - sli:slo2_delivery_success:ratio_rate1h) / (1 - 0.99)",
                    "SLO-2",
                    "B",
                ),
                _target(
                    "(1 - sli:slo3_ws_connect:ratio_rate1h) / (1 - 0.995)",
                    "SLO-3",
                    "C",
                ),
            ],
            _grid(12, 8, 12, 8),
        ),
        # 30-day burn-down: cumulative error ratio vs target line per SLO.
        timeseries(
            "SLI ratio over time (1h windows)",
            [
                _target("sli:slo1_api_availability:ratio_rate1h", "SLO-1", "A"),
                _target("sli:slo2_delivery_success:ratio_rate1h", "SLO-2", "B"),
                _target("sli:slo3_ws_connect:ratio_rate1h", "SLO-3", "C"),
            ],
            _grid(0, 16, 24, 8),
            unit="percentunit",
        ),
    ]
    return dashboard("df-slo-burn", "DataForge — SLO burn & error budget", ["slo"], panels)


def build_control_plane() -> dict:
    panels = [
        timeseries(
            "Request rate by route",
            [_target('sum by (route) (rate(df_http_requests_total{route=~"/api/v1.*"}[5m]))', "{{route}}")],
            _grid(0, 0, 12, 8),
            unit="reqps",
        ),
        timeseries(
            "Error rate (5xx) by route",
            [
                _target(
                    'sum by (route) (rate(df_http_requests_total{route=~"/api/v1.*", status=~"5.."}[5m]))',
                    "{{route}}",
                )
            ],
            _grid(12, 0, 12, 8),
            unit="reqps",
        ),
        timeseries(
            "Latency p50/p90/p99 by route",
            [
                _target(
                    'histogram_quantile(0.50, sum by (route, le) (rate(df_http_request_duration_seconds_bucket{route=~"/api/v1.*"}[5m])))',
                    "p50 {{route}}",
                    "A",
                ),
                _target(
                    'histogram_quantile(0.90, sum by (route, le) (rate(df_http_request_duration_seconds_bucket{route=~"/api/v1.*"}[5m])))',
                    "p90 {{route}}",
                    "B",
                ),
                _target(
                    'histogram_quantile(0.99, sum by (route, le) (rate(df_http_request_duration_seconds_bucket{route=~"/api/v1.*"}[5m])))',
                    "p99 {{route}}",
                    "C",
                ),
            ],
            _grid(0, 8, 12, 8),
            unit="s",
        ),
        stat(
            "Requests in flight",
            [_target("sum(df_http_requests_in_flight)", "in flight")],
            _grid(12, 8, 6, 8),
        ),
        timeseries(
            "Auth failures by mechanism/reason",
            [
                _target(
                    "sum by (mechanism, reason) (rate(df_auth_failures_total[5m]))",
                    "{{mechanism}}/{{reason}}",
                )
            ],
            _grid(18, 8, 6, 8),
            unit="ops",
        ),
        timeseries(
            "Rate limiting by scope",
            [_target("sum by (scope) (rate(df_rate_limited_total[5m]))", "{{scope}}")],
            _grid(0, 16, 8, 8),
            unit="ops",
        ),
        timeseries(
            "Celery queue depth",
            [_target("max by (queue) (df_celery_queue_depth)", "{{queue}}")],
            _grid(8, 16, 8, 8),
        ),
        timeseries(
            "Beat freshness (age since last fire)",
            [
                _target(
                    "time() - max by (schedule) (df_beat_last_run_timestamp_seconds)",
                    "{{schedule}}",
                )
            ],
            _grid(16, 16, 8, 8),
            unit="s",
        ),
    ]
    return dashboard("df-control-plane", "DataForge — Control plane", ["control-plane"], panels)


def build_runner_generation() -> dict:
    panels = [
        timeseries(
            "Aggregate generation TPS by event_class (business vs CDC)",
            [
                _target(
                    "sum by (event_class) (rate(df_generation_events_total[1m]))",
                    "{{event_class}}",
                )
            ],
            _grid(0, 0, 12, 8),
            unit="ops",
        ),
        timeseries(
            "Tick duration p50/p99",
            [
                _target(
                    "histogram_quantile(0.50, sum by (le) (rate(df_runner_tick_duration_seconds_bucket[5m])))",
                    "p50",
                    "A",
                ),
                _target(
                    "histogram_quantile(0.99, sum by (le) (rate(df_runner_tick_duration_seconds_bucket[5m])))",
                    "p99",
                    "B",
                ),
            ],
            _grid(12, 0, 12, 8),
            unit="s",
        ),
        timeseries(
            "Tick overruns",
            [_target("sum(rate(df_runner_tick_overruns_total[5m]))", "overruns/s")],
            _grid(0, 8, 8, 8),
            unit="ops",
        ),
        timeseries(
            "Lease map: held vs streams running",
            [
                _target("sum(df_runner_active_leases)", "active leases", "A"),
                _target("sum(df_runner_streams_running)", "streams running", "B"),
            ],
            _grid(8, 8, 8, 8),
        ),
        timeseries(
            "Lease takeovers by reason",
            [
                _target(
                    "sum by (reason) (rate(df_runner_lease_takeovers_total[10m]))",
                    "{{reason}}",
                )
            ],
            _grid(16, 8, 8, 8),
            unit="ops",
        ),
        stat(
            "Checkpoint age (max)",
            [_target("max(df_checkpoint_age_seconds)", "age")],
            _grid(0, 16, 6, 8),
            unit="s",
        ),
        timeseries(
            "Checkpoint persist duration p99",
            [
                _target(
                    "histogram_quantile(0.99, sum by (le) (rate(df_checkpoint_duration_seconds_bucket[5m])))",
                    "p99",
                )
            ],
            _grid(6, 16, 6, 8),
            unit="s",
        ),
        timeseries(
            "Ledger append health: rate + failures",
            [
                _target("sum(rate(df_ledger_append_failures_total[5m]))", "failures/s", "A"),
                _target(
                    "histogram_quantile(0.99, sum by (le) (rate(df_ledger_append_duration_seconds_bucket[5m])))",
                    "append p99 (s)",
                    "B",
                ),
            ],
            _grid(12, 16, 6, 8),
        ),
        timeseries(
            "Quota system-pauses by reason",
            [_target("sum by (reason) (rate(df_quota_pauses_total[1h]))", "{{reason}}")],
            _grid(18, 16, 6, 8),
            unit="ops",
        ),
    ]
    return dashboard(
        "df-runner-generation", "DataForge — Runner / generation (data plane)", ["data-plane"], panels
    )


def build_kafka_sinks() -> dict:
    panels = [
        timeseries(
            "Kafka publish rate by result",
            [_target("sum by (result) (rate(df_kafka_publish_total[1m]))", "{{result}}")],
            _grid(0, 0, 12, 8),
            unit="ops",
        ),
        timeseries(
            "Kafka publish failures + retries",
            [
                _target(
                    'sum(rate(df_kafka_publish_total{result!="ok"}[5m]))', "failures/s", "A"
                ),
                _target("sum(rate(df_kafka_publish_retries_total[5m]))", "retries/s", "B"),
            ],
            _grid(12, 0, 12, 8),
            unit="ops",
        ),
        timeseries(
            "Consumer lag per group/topic/partition",
            [
                _target(
                    "df_kafka_consumer_lag",
                    "{{group}}/{{topic}}/{{partition}}",
                )
            ],
            _grid(0, 8, 12, 8),
        ),
        stat(
            "Kafka broker reachability (procs up)",
            [_target("sum(df_kafka_broker_up)", "procs reaching broker")],
            _grid(12, 8, 6, 8),
        ),
        timeseries(
            "Consumer fetch rate by group",
            [_target("sum by (group) (rate(df_kafka_consumer_fetch_total[5m]))", "{{group}}")],
            _grid(18, 8, 6, 8),
            unit="ops",
        ),
        heatmap(
            "Buffer commit-lag heatmap (SLO-2 source)",
            "sum by (le) (rate(df_buffer_commit_lag_seconds_bucket[5m]))",
            _grid(0, 16, 12, 8),
        ),
        timeseries(
            "Buffer write rate by result + batch p99",
            [
                _target("sum by (result) (rate(df_buffer_writes_total[5m]))", "{{result}}", "A"),
                _target(
                    "histogram_quantile(0.99, sum by (le) (rate(df_buffer_write_batch_size_bucket[5m])))",
                    "batch p99 (rows)",
                    "B",
                ),
            ],
            _grid(12, 16, 6, 8),
        ),
        timeseries(
            "Buffer oldest-partition age + rows",
            [
                _target("df_buffer_oldest_partition_age_seconds", "oldest age (s)", "A"),
                _target("df_buffer_rows", "rows resident", "B"),
            ],
            _grid(18, 16, 6, 8),
        ),
    ]
    return dashboard("df-kafka-sinks", "DataForge — Backbone & sinks", ["backbone", "sinks"], panels)


def build_ws() -> dict:
    panels = [
        timeseries(
            "WS connect attempts by result",
            [_target("sum by (result) (rate(df_ws_connect_total[5m]))", "{{result}}")],
            _grid(0, 0, 12, 8),
            unit="ops",
        ),
        stat(
            "WS connections active",
            [_target("sum(df_ws_connections_active)", "active")],
            _grid(12, 0, 6, 8),
        ),
        timeseries(
            "WS connect success ratio (SLO-3, 5m)",
            [_target("sli:slo3_ws_connect:ratio_rate5m", "accepted ratio")],
            _grid(18, 0, 6, 8),
            unit="percentunit",
        ),
        timeseries(
            "WS frames sent vs dropped(backpressure)",
            [
                _target("sum(rate(df_ws_frames_sent_total[5m]))", "sent/s", "A"),
                _target(
                    "sum by (reason) (rate(df_ws_frames_dropped_total[5m]))",
                    "dropped {{reason}}/s",
                    "B",
                ),
            ],
            _grid(0, 8, 12, 8),
            unit="ops",
        ),
        timeseries(
            "WS fanout lag p50/p99",
            [
                _target(
                    "histogram_quantile(0.50, sum by (le) (rate(df_ws_fanout_lag_seconds_bucket[5m])))",
                    "p50",
                    "A",
                ),
                _target(
                    "histogram_quantile(0.99, sum by (le) (rate(df_ws_fanout_lag_seconds_bucket[5m])))",
                    "p99",
                    "B",
                ),
            ],
            _grid(12, 8, 12, 8),
            unit="s",
        ),
        heatmap(
            "WS connection duration heatmap",
            "sum by (le) (rate(df_ws_connection_duration_seconds_bucket[5m]))",
            _grid(0, 16, 24, 8),
        ),
    ]
    return dashboard("df-ws", "DataForge — WebSocket delivery", ["ws"], panels)


def build_chaos() -> dict:
    panels = [
        timeseries(
            "Chaos injections by mode",
            [_target("sum by (mode) (rate(df_chaos_injections_total[5m]))", "{{mode}}")],
            _grid(0, 0, 12, 8),
            unit="ops",
        ),
        timeseries(
            "Chaos streams enabled by mode",
            [_target("sum by (mode) (df_chaos_streams_enabled)", "{{mode}}")],
            _grid(12, 0, 12, 8),
        ),
        timeseries(
            "Late-arrival buffer: pending vs overdue",
            [
                _target("df_chaos_late_buffer_pending", "pending", "A"),
                _target("df_chaos_late_buffer_overdue", "overdue", "B"),
            ],
            _grid(0, 8, 12, 8),
        ),
        stat(
            "Late-buffer overdue (alert at >100)",
            [_target("max(df_chaos_late_buffer_overdue)", "overdue")],
            _grid(12, 8, 6, 8),
        ),
        timeseries(
            "Re-emission outcomes",
            [
                _target(
                    "sum by (outcome) (rate(df_chaos_reemissions_total[5m]))",
                    "{{outcome}}",
                )
            ],
            _grid(18, 8, 6, 8),
            unit="ops",
        ),
    ]
    return dashboard("df-chaos", "DataForge — Chaos", ["chaos"], panels)


def main() -> None:
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    builders = {
        "slo-burn.json": build_slo_burn,
        "control-plane.json": build_control_plane,
        "runner-generation.json": build_runner_generation,
        "kafka-sinks.json": build_kafka_sinks,
        "ws.json": build_ws,
        "chaos.json": build_chaos,
    }
    for filename, builder in builders.items():
        path = DASH_DIR / filename
        path.write_text(json.dumps(builder(), indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
