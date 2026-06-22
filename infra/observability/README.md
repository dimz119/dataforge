# DataForge observability artifacts (Phase 11, P11-06)

SLO recording rules, multiwindow burn-rate alert rules, and Grafana dashboards
that turn the `df_` metrics catalog (observability.md §4) into the SLOs (§7),
error-budget burn alerts (§8), the frozen 16-alert catalog (§9), and the frozen
dashboard panel list (§11).

These are **reviewable config artifacts**. Per the Phase 11 build scope they are
validated by `yaml.safe_load` / `json.load` parse only — not executed against a
live Prometheus in this phase. The optional dev compose profile below lets you
run them locally end-to-end if you want.

## Layout

```
infra/observability/
  prometheus/
    prometheus.yml             # dev scrape config + rule_files + (empty) alerting
    slo-recording-rules.yml    # SLO-1/2/3 SLI ratios over 5m/1h/6h/24h/3d/30d
    slo-alerts.yml             # all 16 frozen alerts (3 multiwindow burn + 13 op)
  grafana/
    _generate_dashboards.py    # regenerates the six dashboard JSONs
    dashboards/                # six process-group / SLO dashboards (frozen §11 panels)
      slo-burn.json
      control-plane.json
      runner-generation.json
      kafka-sinks.json
      ws.json
      chaos.json
    provisioning/
      datasources/prometheus.yml   # binds ${DS_PROMETHEUS} -> dev Prometheus
      dashboards/dataforge.yml     # file provider -> "DataForge" folder
  README.md
```

## SLO recording rules (`prometheus/slo-recording-rules.yml`)

Each SLI is recorded as a **good fraction** in `[0,1]` over a fixed window set
(5m, 1h, 6h, 24h, 3d, 30d). 30d is the §7 reporting / error-budget window; the
shorter windows feed the multiwindow burn alerts.

| SLO | SLI recorded | Source metric(s) |
|---|---|---|
| SLO-1 control-plane availability | `1 - 5xx_rate / total_rate` over `route=~"/api/v1.*"` (4xx are GOOD) | `df_http_requests_total{method,route,status}` |
| SLO-1 latency companion | `le="0.5"` bucket / total for GET; `le="1"` for writes | `df_http_request_duration_seconds` |
| SLO-2 delivery success | `le="30"` bucket / total of the commit-lag histogram | `df_buffer_commit_lag_seconds` |
| SLO-3 WS connect | `accepted / (accepted+error+timeout)` (auth_failed, quota_rejected excluded) | `df_ws_connect_total{result}` |

Record names follow `sli:<slo>:ratio_rate<window>`. Targets are recorded once as
`sli:<slo>:target` (SLO-1 99.5%, SLO-2 99.0%, SLO-3 99.5%) — the alert rules
inline the same numeric budgets so a stale recording rule can't silently change
a paging threshold.

## Alert rules (`prometheus/slo-alerts.yml`)

All **16 frozen alert names** (observability.md §9). The three SLO alerts use the
Google-SRE multiwindow / multi-burn-rate pattern (§8): each emits four rules
(one per tier) sharing the frozen `alertname`, distinguished by a `burn_window`
label.

Burn rate = `(1 - SLI) / (1 - target)`.

| Tier | Long window | Short confirm | Threshold | `for` | Severity |
|---|---|---|---|---|---|
| Fast | 1h | 5m | > 14.4x | 2m | page |
| Medium | 6h | 1h | > 6x | 15m | page |
| Slow | 24h | 6h | > 3x | 1h | ticket |
| Trickle | 3d | 6h | > 1x | 3h | ticket |

The 16 alerts and their conditions:

| Alert | Severity | Condition |
|---|---|---|
| `ApiAvailabilityBurn` | page | SLO-1 multiwindow burn |
| `DeliveryFreshnessBurn` | page | SLO-2 multiwindow burn |
| `WsConnectBurn` | page | SLO-3 multiwindow burn |
| `ApiLatencyP99` | ticket | route p99 > 2x SLO bound (GET>1s or write>2s), 15m |
| `KafkaBrokerDown` | page | `count(df_kafka_broker_up == 0) >= 2`, 1m |
| `ConsumerLagGrowing` | page | `min by(group)(deriv(df_kafka_consumer_lag[10m])) > 0`, 10m |
| `RunnerLeaseTakeoverSpike` | page | `rate(df_runner_lease_takeovers_total[10m]) > 3` |
| `StreamFailoverExhausted` | page | `increase(df_stream_failover_exhausted_total[5m]) > 0` (log-derived) |
| `LateBufferOverdue` | page | `df_chaos_late_buffer_overdue > 100`, 5m |
| `CheckpointStale` | ticket | `df_checkpoint_age_seconds > 300` |
| `BufferRetentionStalled` | ticket | `df_buffer_oldest_partition_age_seconds > 54h` (retention 48h + 6h) |
| `BeatDead` | page | `time() - max(df_beat_last_run_timestamp_seconds) > 120` (2x 60s schedule) |
| `CeleryQueueBacklog` | ticket | `max(df_celery_queue_depth) > 1000`, 15m |
| `AuthFailureSpike` | ticket | `rate(df_auth_failures_total[5m]) > 50` |
| `QuotaPauseSpike` | ticket | `rate(df_quota_pauses_total[1h]) > 20/h` |
| `CursorExpiredSpike` | ticket | `rate(df_cursor_expired_total[1h]) > 50/h` |

> `StreamFailoverExhausted` is **log-derived** (§9): a stream entering `failed`
> with `failover_exhausted` is not in the `df_` §4 catalog. The rule references
> `df_stream_failover_exhausted_total`, which a log-to-metric exporter (or a
> future small counter wired by the failover path) must expose. The rule + a
> runbook exist now so the page is wired the moment that counter does.

Every `page`-severity alert carries a `runbook:` annotation pointing at
`infra/runbooks/<AlertName>.md` (P11-13 writes those files).

## Dashboards (`grafana/dashboards/`)

Six dashboards matching the frozen §11 panel list (process-group split; the §11
"Backbone & sinks" WS panels live in the dedicated `ws` dashboard):

| File | §11 dashboard | Panels (summary) |
|---|---|---|
| `slo-burn.json` | SLO overview | 3 SLI gauges, budget-remaining, current burn rate, SLI-over-time |
| `control-plane.json` | Control plane | req rate / 5xx rate / p50-p99 by route; in-flight; auth failures; rate limiting; Celery queues; beat freshness |
| `runner-generation.json` | Data plane | gen TPS by class; tick duration + overruns; lease map; lease takeovers; checkpoint age/duration; ledger append health; quota pauses |
| `kafka-sinks.json` | Backbone & sinks | publish rate/failures/retries; consumer lag per group/partition; broker reachability; commit-lag heatmap; buffer writes/oldest-age/rows |
| `ws.json` | Backbone & sinks (WS) | connect by result; active; connect ratio; frames sent/dropped; fanout lag; connection-duration heatmap |
| `chaos.json` | Chaos | injections by mode; streams enabled; late-buffer pending/overdue; re-emission outcomes |

Regenerate after editing the builder:

```bash
python infra/observability/grafana/_generate_dashboards.py
```

The dashboards use a templated `${DS_PROMETHEUS}` datasource so they import
cleanly into any Grafana; the provisioning datasource binds it to the dev
Prometheus (`uid: dataforge-prometheus`).

## Dev compose profile (optional, runs the artifacts end-to-end)

The artifacts are wired into the main dev stack behind an optional
`observability` profile (off by default):

```bash
# bring the core stack up first (or alongside)
docker compose -f infra/compose/compose.yaml up -d --wait

# then add Prometheus + Grafana
docker compose -f infra/compose/compose.yaml --profile observability up -d
```

- **Prometheus** at <http://localhost:9090> — mounts the three rule/config files
  read-only; check the *Status -> Rules* and *Alerts* tabs to confirm the
  recording rules and all 16 alerts loaded. It scrapes **one target per process
  group** (foundation note): `api:8000/metrics` and `ws:8001/metrics` via the
  shared web/ASGI ports; `worker`, `runner`, and `buffer-writer` on the
  side-port `DF_METRICS_PORT` (default `9091`). The runner process hosts the
  buffer-writer + ws-pusher threads, so all three families share that one
  exposer — modelled here as the single `sinks` / `runner` targets.
- **Grafana** at <http://localhost:3000> (anonymous Admin in dev) — the six
  dashboards auto-provision into the **DataForge** folder.

To expose the side ports for local scraping set `DF_METRICS_PORT=9091` in
`infra/compose/.env` and publish `9091` on `worker`/`runner`/`buffer-writer` (in
dev these processes already start the exposer when `DF_METRICS_PORT > 0`; the
compose `expose`/`ports` mapping is the only addition needed for cross-container
scraping).

## Production scraping (M-1)

In production these rules + dashboards are loaded by the managed/Fly Prometheus,
which scrapes each process group via the `fly.toml [[metrics]]` block (P11-10
owns the fly.toml metrics target wiring). Fly's `[[metrics]]` maps the web
tier's `/metrics` HTTP path target **separately** from the side-port (9091)
targets of the `worker`/`runner`/`sinks` process groups — same one-exposer-per-
process-group model as the dev profile above.

## Validation

```bash
# YAML rules parse
python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" \
  infra/observability/prometheus/*.yml \
  infra/observability/grafana/provisioning/datasources/*.yml \
  infra/observability/grafana/provisioning/dashboards/*.yml

# Grafana JSON parse
python -c "import json,sys; [json.load(open(f)) for f in sys.argv[1:]]" \
  infra/observability/grafana/dashboards/*.json
```
