# DataForge — Observability

**Deliverable:** supporting spec for D2 / D13 / D15 (observability NFRs; SLO owner)

This document defines how DataForge is observed: the structured JSON log schema every process emits, the trace-correlation conventions and the OpenTelemetry adoption path, the Prometheus metrics catalog across both planes, the tenant-facing stream stats surface, `/healthz` vs `/readyz` semantics per process, the SLO definitions for control-plane availability, data-plane delivery, and WebSocket connect success (closing the panel gap on what "99.9%" measures), the error-budget policy and alert catalog, and the audit-event catalog. It binds to the process inventory in [system-architecture.md](system-architecture.md) §6, the failure domains in [system-architecture.md](system-architecture.md) §9, and the Observation/Audit contexts in [../03-domain/domain-model.md](../03-domain/domain-model.md) §2.9–2.10. Phase 11 ships the full dashboard/alerting/runbook surface; the contracts below are frozen now so every earlier phase emits against them.

---

## 1. Principles

| # | Principle | Consequence |
|---|---|---|
| O-1 | **Tenant context everywhere it is cheap, nowhere it explodes.** `workspace_id`/`stream_id` are mandatory log fields and Redis stats dimensions; they are **banned as Prometheus label values** (unbounded cardinality). | §2, §4.1 |
| O-2 | **Logs are events, not prose.** Single-line JSON to stdout, machine-parseable, with a stable `event` name; humans read dashboards, not grep. | §2 |
| O-3 | **The hot path is metered, not logged.** No per-event log lines at INFO; per-event visibility comes from metrics, stream stats, and the ledger/injection records themselves. | §2.4 |
| O-4 | **Observation never mutates.** All surfaces here are read-only over domain state (INV-OBS-1); idle-detection signals feed Stream Control, which owns the pause command. | domain model §2.9 |
| O-5 | **SLOs are honest.** Targets reflect the single-region, single-broker MVP reality; 99.9% is a post-GA roadmap item with a defined upgrade path, not a banner claim. | §7 |
| O-6 | **Correlation is designed in now, exported later.** W3C trace ids ride every log from Phase 1; OTLP export is a post-GA bolt-on, not a refactor. | §3 |

---

## 2. Structured logging

### 2.1 Transport and format

Every process (`web`, `ws`, `worker`, `beat`, `runner`, `buffer-writer`, `ws-pusher`) writes **one JSON object per line to stdout**, UTF-8, no multi-line records (stack traces are escaped into a field). Fly's log shipping collects stdout; dev Compose uses `docker compose logs` plus an optional local Loki profile. Library: `structlog` with a shared processor chain defined once in the backend config package — no process configures logging independently.

Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Production default is `INFO`; per-logger overrides via `DF_LOG_LEVELS` (e.g. `dataforge.chaos=DEBUG`). `DEBUG` is never enabled globally in production.

### 2.2 Field schema (frozen; additive evolution only)

| Field | Type | Required | Semantics |
|---|---|---|---|
| `ts` | string, RFC 3339 UTC, ms precision | always | Wall-clock emission time |
| `level` | string enum | always | `debug`/`info`/`warning`/`error`/`critical` |
| `event` | string | always | Stable machine-readable name, dotted snake_case: `http.request.completed`, `runner.tick.completed`, `lease.takeover`, `sink.batch.committed`, `auth.key.rejected` |
| `message` | string | always | Human-readable one-liner |
| `logger` | string | always | Python logger path (`dataforge.streams.api`) |
| `service` | string enum | always | Process role: `web`, `ws`, `worker`, `beat`, `runner`, `buffer-writer`, `ws-pusher` |
| `env` | string enum | always | `dev`, `staging`, `prod` |
| `release` | string | always | Image tag / git SHA |
| `request_id` | string, UUIDv7 | when in a request/task context | One per inbound HTTP/WS request or Celery task; returned to clients as the `X-Request-Id` response header; stored on audit entries (domain model §2.10) |
| `trace_id` | string, 32 lowercase hex | when in a traced context | W3C Trace Context trace id (§3) |
| `span_id` | string, 16 lowercase hex | when in a traced context | Current span |
| `workspace_id` | string UUID \| null | **required (nullable)** | The tenant in whose context the line was emitted; `null` only for platform-scoped lines (boot, scheduler, broker probes) |
| `stream_id` | string UUID \| null | required (nullable) | Set on every stream-scoped line (runner ticks, sink batches, stream commands) |
| `shard_id` | integer \| null | optional | Set wherever `stream_id` is set in data-plane processes |
| `user_id` | string UUID \| null | optional | Acting console user, if any |
| `api_key_id` | string UUID \| null | optional | Acting key — **the key id and prefix only, never secret or hash** |
| `duration_ms` | number | optional | For completed-operation events |
| `status` | integer \| string | optional | HTTP status, task state, or outcome enum |
| `error.kind` / `error.message` / `error.stack` | strings | on `error`+ | Exception class, message, escaped traceback |
| `ctx` | object | optional | Event-specific payload (bounded: ≤ 32 keys, ≤ 8 KiB serialized) |

Redaction rules (enforced by a shared processor, tested in CI): never log API-key secrets or hashes, JWTs, passwords, password hashes, verification/reset tokens, or `Authorization` headers; API keys appear as `prefix…last4` only. Event **payloads** are never logged at INFO or above (O-3); at DEBUG in dev they may appear truncated to 2 KiB.

### 2.3 Examples (normative shape)

```json
{"ts":"2026-06-10T14:23:05.301Z","level":"info","event":"http.request.completed",
 "message":"GET /api/v1/streams/7b1e…/events 200 (38ms)","logger":"delivery.api",
 "service":"web","env":"prod","release":"a1b2c3d","request_id":"019ea1c5-77aa-7000-8000-aabbccddeeff",
 "trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7",
 "workspace_id":"0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60","stream_id":"7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b",
 "user_id":null,"api_key_id":"3f9e8d7c-6b5a-4321-9876-0fedcba98765",
 "duration_ms":38,"status":200,"ctx":{"route":"streams-events-list","events_returned":500,"cursor_age_s":4}}
```

```json
{"ts":"2026-06-10T14:23:06.020Z","level":"info","event":"runner.tick.summary",
 "message":"stream 7b1e… shard 0: 612 events in last 60s (10.2 tps)","logger":"dataforge.generation.runner",
 "service":"runner","env":"prod","release":"a1b2c3d","request_id":null,
 "trace_id":"7c1de2a90b34da6a3ce929d0e0e47abc","span_id":"1a2b3c4d5e6f7081",
 "workspace_id":"0d9f7b42-3a61-4c2e-9b8f-5e1a2c3d4f60","stream_id":"7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b",
 "shard_id":0,"ctx":{"events_60s":612,"observed_tps":10.2,"virtual_now":"2026-06-10T14:23:05Z",
 "lease_age_s":3621,"checkpoint_age_s":12,"late_buffer_pending":4}}
```

### 2.4 Volume rules

| Rule | Statement |
|---|---|
| LV-1 | Data-plane processes emit **at most one INFO line per stream per 60 s** (the tick summary above); individual ticks and events log only at DEBUG. |
| LV-2 | Chaos injections are never logged per-injection at INFO — they are InjectionRecord rows and the `df_chaos_injections_total` counter. |
| LV-3 | Every state transition of a stream's lifecycle (§ domain model 4.2) logs exactly one INFO line (`stream.state.changed`) with from/to/reason. |
| LV-4 | `WARNING`+ is unbounded by sampling (problems must not be sampled away) but deduplicated by a 60 s `(event, workspace_id, stream_id)` suppression window with a `suppressed_count` on the next emission. |

---

## 3. Trace correlation and the OpenTelemetry adoption path

### 3.1 Now (Phases 1–11): correlation without an exporter

| Mechanism | Contract |
|---|---|
| Trace context | `web` and `ws` accept an inbound W3C `traceparent` header (generate one if absent) and put `trace_id`/`span_id` in every log line of that request. |
| `request_id` | UUIDv7 minted at ingress per request; echoed as `X-Request-Id`; recorded on audit entries — the support-ticket join key. |
| Control → async propagation | Celery tasks carry `request_id` and `traceparent` in task headers; task logs continue the same `trace_id`. |
| Control → data-plane propagation | Lifecycle commands write `request_id` into the desired-state document; the runner logs `stream.state.changed` with that `request_id`, linking a user's click to the runner's convergence. Runner ticks otherwise start fresh `trace_id`s (one per tick) — event-level correlation uses `event_id`/`correlation_id` from the envelope, not traces (volume, O-3). |
| Sinks | Each consumed batch logs its Kafka topic/partition/offset range in `ctx`, joining delivery questions to broker positions. |

### 3.2 Later (post-GA): OTLP export

| Step | Trigger | Change |
|---|---|---|
| 1. Traces | First post-GA quarter, or earlier if incident debugging cost demands it | Adopt `opentelemetry-sdk`: auto-instrument Django, DRF, Celery, psycopg, redis-py, confluent-kafka; export OTLP/HTTP to a hosted collector. Sampling: parent-based 10% head sampling plus error-biased tail sampling. Log fields are already named `trace_id`/`span_id` (W3C format), so log↔trace joins work with zero log-schema change. |
| 2. Metrics | After step 1 settles | Route metrics through the OTel SDK with the Prometheus exporter, preserving every metric name in §4 verbatim (the catalog is the contract, not the SDK). |
| 3. Cross-plane spans | With step 1 | Span links from the lifecycle-command span to the runner convergence span via the propagated `traceparent` in desired state. |

Nothing in MVP code awaits OTel: the adoption is additive configuration plus dependency, which is the entire point of pinning field names and metric names now.

---

## 4. Metrics

### 4.1 Conventions

| Rule | Statement |
|---|---|
| M-1 | Prometheus text format on `DF_METRICS_PORT` (default 9091) `/metrics`, every process. Fly's built-in Prometheus scrapes per `fly.toml` `[[metrics]]`; dev Compose ships an optional `prometheus` + `grafana` profile. |
| M-2 | Names: `df_` prefix, snake_case, unit suffix (`_seconds`, `_bytes`, `_total` for counters). Histograms declare explicit buckets below. |
| M-3 | **Cardinality rule (binding):** allowed label values must be bounded and enumerable at deploy time. `workspace_id`, `stream_id`, `user_id`, `event_id` are banned as labels. Per-tenant numbers live in stream stats (§5) and logs (§2). Per-tenant *aggregates* exposed to a tenant never include another tenant's data (INV-OBS-3). |
| M-4 | Every process also exports the standard Python process metrics (`process_*`, GC, open fds) via the default client collectors. |
| M-5 | Default histogram buckets — HTTP and sink commit: `.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10` s; tick/publish/append: `.001,.0025,.005,.01,.025,.05,.1,.25,.5,1,2.5` s; lag: `.1,.5,1,2.5,5,10,30,60,300,1800` s. |

### 4.2 Control-plane API (`web`)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_http_requests_total` | counter | `method`, `route` (DRF route name, not raw path), `status` | Traffic + availability SLI numerator/denominator (§7.1) |
| `df_http_request_duration_seconds` | histogram | `method`, `route` | Latency SLI |
| `df_http_requests_in_flight` | gauge | — | Saturation |
| `df_auth_failures_total` | counter | `mechanism` (`jwt`,`api_key`), `reason` (`expired`,`revoked`,`unknown`,`scope`) | Abuse and misconfiguration signal |
| `df_rate_limited_total` | counter | `scope` (`key`,`ip`,`signup`) | Rate-limit pressure |
| `df_cursor_expired_total` | counter | — | `410 cursor-expired` responses (INV-DEL-4) — a spike means consumers are slower than retention |
| `df_events_served_total` | counter | `channel="rest"` | Events returned by the cursor API (consumption volume) |

### 4.3 Celery (`worker`, `beat`)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_celery_tasks_total` | counter | `task`, `state` (`succeeded`,`failed`,`retried`) | Control-plane job health |
| `df_celery_task_duration_seconds` | histogram | `task` | Long-running job watch (backfills, snapshots) |
| `df_celery_queue_depth` | gauge | `queue` | Backlog; alert input |
| `df_beat_last_run_timestamp_seconds` | gauge | `schedule` | Detects a dead scheduler (partition drops, retention jobs) |

### 4.4 Runner and generation

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_runner_active_leases` | gauge | — | Shards this process holds; fleet sum vs desired = convergence health |
| `df_runner_streams_running` | gauge | — | Running streams hosted here |
| `df_runner_lease_takeovers_total` | counter | `reason` (`expiry`,`rebalance`) | Failover frequency (system-architecture §9: kill-test path) |
| `df_runner_tick_duration_seconds` | histogram | — | Tick budget; saturation precursor |
| `df_runner_tick_overruns_total` | counter | — | Ticks exceeding the tick interval — the first sign a shard is at its ceiling ([scaling-strategy.md](scaling-strategy.md)) |
| `df_generation_events_total` | counter | `event_class` (`business`,`cdc`) | Canonical generation volume |
| `df_ledger_append_duration_seconds` | histogram | — | Hop budget (system-architecture §5.3) |
| `df_ledger_append_failures_total` | counter | — | Postgres trouble seen from the data plane |
| `df_checkpoint_duration_seconds` | histogram | — | Checkpoint cost (30 s cadence) |
| `df_checkpoint_age_seconds` | gauge | — | Max age across held shards; recovery-point exposure |
| `df_pool_entities` | gauge | `entity_class` (`actor`,`other`) | Hot-state footprint vs B-08/B-09 bounds |
| `df_quota_pauses_total` | counter | `reason` (`quota`,`idle`) | System-pause volume (PRD §7 behavior) |

### 4.5 Kafka backbone

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_kafka_publish_total` | counter | `result` (`acked`,`failed`) | Publish health from runners |
| `df_kafka_publish_duration_seconds` | histogram | — | Hop budget |
| `df_kafka_publish_retries_total` | counter | — | Broker pressure precursor |
| `df_kafka_consumer_lag` | gauge | `group` (`df.sink.rest-buffer.v1`,`df.sink.websocket.v1`), `topic`, `partition` | **The** data-plane backlog signal; alert input; Phase 6 soak gate ("no consumer-lag growth") |
| `df_kafka_consumer_fetch_total` | counter | `group` | Sink liveness |
| `df_kafka_broker_up` | gauge | — | Probe result from each connected process (1/0) |

### 4.6 Chaos

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_chaos_injections_total` | counter | `mode` (the seven canonical mode names) | Injection volume; statistical-test cross-check input (Phase 9 ±1% gates) |
| `df_chaos_streams_enabled` | gauge | `mode` | How much chaos is live platform-wide |
| `df_chaos_late_buffer_pending` | gauge | — | Scheduled re-emissions not yet due |
| `df_chaos_late_buffer_overdue` | gauge | — | Entries past `due_at` and not emitted — must hover near 0; growth means the re-emission scheduler is sick (INV-CHA-5 risk) |
| `df_chaos_reemissions_total` | counter | `outcome` (`emitted`,`discarded_on_stop`,`flushed_on_stop`) | Late-buffer lifecycle accounting vs OnStopPolicy |

### 4.7 Buffer and REST delivery

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_buffer_writes_total` | counter | `result` (`committed`,`failed`) | Sink write health |
| `df_buffer_write_batch_size` | histogram | — (buckets 1,10,50,100,500,1000,5000) | Batching efficiency |
| `df_buffer_commit_lag_seconds` | histogram | — | `persisted_at − emitted_at` per event at commit; **data-plane SLI source** (§7.2) |
| `df_buffer_oldest_partition_age_seconds` | gauge | — | Retention-job health; > retention + 6 h means drop jobs are failing |
| `df_buffer_partitions_dropped_total` | counter | — | Retention job activity |
| `df_buffer_rows` | gauge | — | Buffer volume (capacity planning input) |

### 4.8 WebSocket delivery (`ws`, `ws-pusher`)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `df_ws_connect_total` | counter | `result` (`accepted`,`auth_failed`,`quota_rejected`,`error`,`timeout`) | **WS connect SLI source** (§7.3) |
| `df_ws_connections_active` | gauge | — | Concurrent sockets |
| `df_ws_frames_sent_total` | counter | — | Tail volume |
| `df_ws_frames_dropped_total` | counter | `reason` (`backpressure`) | Drop-oldest events (INV-DEL-5); each drop also sends the client a drop-notice frame |
| `df_ws_fanout_lag_seconds` | histogram | — | Kafka publish → frame sent (hop budget) |
| `df_ws_connection_duration_seconds` | histogram | — (buckets 1,10,60,300,1800,7200 s) | Session length; reconnect-storm detection |

---

## 5. Stream stats (tenant-facing observability)

Stream stats are the **product surface** of observability — what the console Monitoring page and `GET /api/v1/streams/{id}/stats` show. They are Redis-resident, rebuildable, and workspace-scoped (domain model §2.9).

| Aspect | Contract |
|---|---|
| Storage | Redis hash per stream: `df:ws:{workspace_id}:stream:{stream_id}:stats` plus a sorted-set ring for the TPS window |
| Fields | `total_events`, `events_by_type.{event_type}`, `observed_tps` (10 s sliding window), `last_event_at` (wall), `last_sequence_no` per shard, `chaos_injections_by_mode.{mode}` (counts only — details stay behind the answer key, SB-4) |
| Writer | The runner's publish stage increments counters per **delivered instance** published to Kafka (post-chaos, including duplicates and re-emissions) — stats therefore reconcile with an independent consumer-side tally, the Phase 6 exit criterion |
| Staleness | ≤ 5 s under normal operation (INV-OBS-2) |
| Rebuild | Counters are derivable from the buffer + injection records; a `rebuild_stream_stats` Celery task reconstructs after Redis loss (system-architecture §9: Redis failure row) |
| Isolation | Key prefix carries `workspace_id`; the stats API is scoped + RLS-backed like every tenant read (INV-OBS-3) |

---

## 6. Health and readiness

### 6.1 Semantics

| Endpoint | Question answered | Checks | Consumer | Failure consequence |
|---|---|---|---|---|
| `/healthz` (liveness) | "Is this process alive and not wedged?" | Process up, event loop / worker loop responsive (heartbeat updated within 2× its interval). **No dependency checks** — a dead Postgres must not cause restart storms. | Fly health checks, Compose `healthcheck` | Process restart |
| `/readyz` (readiness) | "Should this process receive work right now?" | Dependency probes with per-process gating sets (below); each probe has a 2 s timeout, results cached 5 s | Deploy gating, LB routing, dashboards | Traffic withheld / deploy halted |

`/readyz` response shape (HTTP 200 iff all *gating* components pass; 503 otherwise):

```json
{"status":"ready",
 "components":{"postgres":"ok","redis":"ok","kafka":"ok","migrations":"ok"},
 "gating":["postgres","redis","migrations"],
 "release":"a1b2c3d"}
```

Non-gating components are reported (the Phase 1 exit criterion "readyz reports green for pg/redis/kafka" reads this map) but do not flip readiness — e.g. a Kafka outage must not take the control-plane API out of rotation.

### 6.2 Per-process gating sets

| Process | Probe surface | Gating (503 if failing) | Reported, non-gating |
|---|---|---|---|
| `web` | service port | `postgres` (`SELECT 1`), `redis` (`PING`), `migrations` (no unapplied) | `kafka` (broker metadata) — REST reads come from Postgres, so the API stays up through broker incidents |
| `ws` | service port | `redis` (channel layer + auth cache), `postgres` (key auth) | `kafka` |
| `worker` | health sidecar `:8092` ([deployment-architecture.md](deployment-architecture.md) §3.2) | `redis` (broker), `postgres` | `kafka` |
| `beat` | same surface as `worker` (supervised in-process) | `redis` | — |
| `runner` | internal aiohttp listener `:8081` ([backend-architecture.md](backend-architecture.md) §8.1) | `postgres` (ledger/desired state), `redis` (leases/pools), `kafka` (publish path) — a runner that cannot publish must not claim leases | — |
| buffer-writer sink | `:8081` (sink-host listener; dev `buffer-writer` container) | `kafka` (consume), `postgres` (buffer writes) | `redis` |
| ws-pusher sink | `:8081` (same listener) | `kafka`, `redis` (channel layer) | `postgres` |

---

## 7. Service level objectives

This section closes the panel gap: it states **what is measured**, the **MVP targets the single-region architecture can honestly meet**, and the **path to 99.9%**. The requirements' 99.9% availability figure is a post-GA roadmap target, not an MVP claim — single-region Fly, one Kafka broker on one volume, and one Postgres primary make a 43.2-minute monthly error budget arithmetically dishonest (one broker-host maintenance event or one Postgres failover can spend it). [system-architecture.md](system-architecture.md) §9 lists the platform-wide failure domains behind this statement.

All SLOs use a **30-day rolling window**. SLIs are computed from the §4 metrics by recording rules; the SLO dashboard and burn alerts ship in Phase 11.

### 7.1 SLO-1 — Control-plane API availability

| Aspect | Definition |
|---|---|
| SLI | `good / total` over requests to `/api/v1/*` on `web`. **Bad** = status ≥ 500, or no response within 30 s. 4xx (including 401/403/404/410/429) are *good* — they are the API working as specified. |
| Source | `df_http_requests_total` (+ a gateway-timeout counter from the LB probe job) |
| Latency SLO (companion) | 99% of read requests (`GET`) complete < 500 ms; 99% of writes < 1 s, measured at the app (`df_http_request_duration_seconds`) |
| **MVP target** | **99.5%** availability (216 min/month budget) |
| Post-GA target | **99.9%** (43.2 min/month) once the §7.4 ladder is climbed |

### 7.2 SLO-2 — Data-plane delivery success

| Aspect | Definition |
|---|---|
| SLI | Fraction of delivered-stream events that become **visible to the REST cursor API within 30 s** of their canonical `emitted_at`. Measured as the share of events with `df_buffer_commit_lag_seconds` ≤ 30. **Exclusion:** chaos `late_arriving` re-emissions are measured against their scheduled `due_at`, not the canonical `emitted_at` — intentional lateness is the product working, not failing. |
| Source | `df_buffer_commit_lag_seconds` histogram (≤ 30 s bucket ratio) |
| Companion (absolute, not a percentage) | **Durability invariant:** zero loss of canonical events within ledger retention and zero loss of delivered events within buffer retention. A durability violation is an incident and a release blocker, never budget spend. |
| **MVP target** | **99.0%** of events within 30 s (the single Kafka broker is in this path; its restarts stall delivery platform-wide, system-architecture §9) |
| Post-GA target | 99.5% after managed Kafka; **99.9%** with the full §7.4 ladder |

### 7.3 SLO-3 — WebSocket connect success

| Aspect | Definition |
|---|---|
| SLI | `accepted / (accepted + error + timeout)` from `df_ws_connect_total`: the fraction of **validly authenticated** WS handshakes that reach the subscribed state (first protocol frame sent) within 3 s. `auth_failed` and `quota_rejected` are excluded from the denominator — they are correct rejections. |
| Source | `df_ws_connect_total` |
| Companion | Frame-drop transparency: 100% of backpressure drops are signaled with a drop-notice frame (INV-DEL-5) — contract-tested, not budgeted. |
| **MVP target** | **99.5%** |
| Post-GA target | 99.9% with ≥ 2 `ws` instances + connection draining on deploy |

### 7.4 The path to 99.9% (post-GA availability ladder)

Each rung removes one platform-wide failure domain from [system-architecture.md](system-architecture.md) §9.2; capacity arithmetic and sequencing live in [scaling-strategy.md](scaling-strategy.md), deployment mechanics in [deployment-architecture.md](deployment-architecture.md).

| Rung | Change | Failure domain removed | SLO unlocked |
|---|---|---|---|
| 1 | Managed Kafka (executes the ADR-0015 pre-committed trigger: external channel ships, sustained TPS > ~5k, **or SLO-2 breach caused by broker incidents**) | Single-broker Kafka | SLO-2 → 99.5% |
| 2 | Postgres HA: managed primary + standby with automated failover; PgBouncer in front | Single Postgres primary (largest blast radius) | SLO-1 → 99.9% credible |
| 3 | ≥ 2 instances per process group + zero-downtime (blue-green) deploys with WS draining | Deploy-induced downtime | SLO-3 → 99.9% |
| 4 | Multi-region `web`/`ws` with regional read replicas; runners stay single-region (generation is region-affine to its stores) | Fly region (for the control plane and consumption) | SLO-1/SLO-2 → 99.9% jointly defensible |

Until rung 1, the honest public posture is the MVP targets of §7.1–7.3, published on the status page as such.

---

## 8. Error budget policy

| Aspect | Policy |
|---|---|
| Budget | `1 − target` over the 30-day rolling window, per SLO. At MVP targets: SLO-1 = 216 min, SLO-2 = 1% of events, SLO-3 = 0.5% of valid handshakes. |
| Burn-rate alerts | Google-SRE multiwindow: **page** when burn rate > 14.4× over 1 h **and** > 14.4× over the trailing 5 min (2% of budget/hour); **page** at > 6× over 6 h; **ticket** at > 3× over 24 h; **ticket** at > 1× over 3 d. Applied to each SLO's SLI recording rule. |
| > 50% budget consumed mid-window | Risky deploys (schema migrations, broker config, runner changes) require an explicit review note in the PR; feature deploys continue. |
| Budget exhausted | Change freeze on the affected plane (control or data) except reliability fixes, until the rolling window recovers above target. The plane split matters: a data-plane freeze does not stop console work, and vice versa. |
| Durability violations | Outside the budget system entirely (§7.2): any canonical-event loss or cross-tenant leak is a sev-1 incident + post-mortem + release blocker regardless of remaining budget. |
| Review | SLO targets and budget policy are reviewed at GA + 90 days against the PRD §8 success metrics, and at every §7.4 rung. |

---

## 9. Alert catalog

Severity: `page` (human now) / `ticket` (next business day). Every `page` has a runbook entry; runbooks ship in Phase 11 (`infra/runbooks/`, one per alert name below — the names are frozen now).

| Alert | Condition (sustained) | Severity | Failure-domain link |
|---|---|---|---|
| `ApiAvailabilityBurn` | SLO-1 multiwindow burn (§8) | page | web / Postgres |
| `ApiLatencyP99` | route p99 > 2× its SLO bound, 15 min | ticket | web |
| `DeliveryFreshnessBurn` | SLO-2 multiwindow burn | page | Kafka / sinks / Postgres |
| `WsConnectBurn` | SLO-3 multiwindow burn | page | ws / Redis |
| `KafkaBrokerDown` | `df_kafka_broker_up == 0` from ≥ 2 processes, 1 min | page | Kafka (platform-wide) |
| `ConsumerLagGrowing` | `df_kafka_consumer_lag` monotone increase 10 min per group | page | sinks |
| `RunnerLeaseTakeoverSpike` | `rate(df_runner_lease_takeovers_total[10m])` > 3 | page | runner fleet |
| `StreamFailoverExhausted` | any stream enters `failed` with `failover_exhausted` (log-derived) | page | runner / stores |
| `LateBufferOverdue` | `df_chaos_late_buffer_overdue > 100` for 5 min | page | chaos scheduler (INV-CHA-5) |
| `CheckpointStale` | `df_checkpoint_age_seconds > 300` | ticket | runner |
| `BufferRetentionStalled` | `df_buffer_oldest_partition_age_seconds` > retention + 6 h | ticket | worker (drop jobs) |
| `BeatDead` | `df_beat_last_run_timestamp_seconds` stale > 2× schedule | page | beat singleton |
| `CeleryQueueBacklog` | `df_celery_queue_depth > 1000` for 15 min | ticket | worker |
| `AuthFailureSpike` | `rate(df_auth_failures_total[5m])` > 50 | ticket | abuse signal → [../06-quality/security-architecture.md](../06-quality/security-architecture.md) |
| `QuotaPauseSpike` | `rate(df_quota_pauses_total[1h])` > 20 | ticket | product signal, not infra |
| `CursorExpiredSpike` | `rate(df_cursor_expired_total[1h])` > 50 | ticket | consumers slower than retention — docs/teaching signal |

---

## 10. Audit-event catalog

Audit entries are the immutable who-did-what record (domain model §2.10): append-only, transactional with the mutation (INV-AUD-2), secret-free (INV-AUD-3), workspace-scoped visibility (INV-AUD-4), retained **400 days** (workspace deletion tombstones, never drops, INV-TEN-6). Action names are `{context}.{object}.{verb}` (past tense). This catalog is the minimum set — extended, never reduced; PRD §8 success metrics are computed from these rows plus stream stats.

| Action | Actor | `workspace_id` | Metadata keys (in addition to target ref + `request_id`) |
|---|---|---|---|
| `identity.user.registered` | user | null | `email_domain` (domain only, not the address) |
| `identity.user.login_succeeded` / `identity.user.login_failed` | user / anonymous | null | `mechanism:"jwt"`, failure `reason` |
| `identity.user.email_verified` | user | null | — |
| `identity.user.password_reset_requested` / `identity.user.password_reset_completed` | user | null | — |
| `identity.user.deleted` | user | null | `memberships_removed` |
| `tenancy.workspace.created` / `tenancy.workspace.deleted` | user | set | `plan_tier`; deletion: `streams_stopped`, `keys_revoked` |
| `tenancy.membership.added` / `tenancy.membership.removed` / `tenancy.membership.role_changed` | admin | set | `member_user_id`, `role` (new), `previous_role` |
| `tenancy.api_key.created` | member | set | `key_prefix`, `scopes`, `expires_at` |
| `tenancy.api_key.revoked` / `tenancy.api_key.expired` | admin/creator / system | set | `key_prefix`, revoke `reason` (`user`,`workspace_deleted`) |
| `catalog.scenario_instance.created` / `catalog.scenario_instance.updated` | member | set | `scenario_slug`, `manifest_version`, `overrides_changed` (key list) |
| `catalog.manifest_version.published` / `catalog.manifest_version.deprecated` | operator/admin | set or null (global scenarios) | `scenario_slug`, `version`, `validation_status` |
| `registry.schema_version.registered` | system (derivation) / operator | set or null | `subject`, `version` |
| `streams.stream.created` / `streams.stream.deleted` | member | set | `scenario_instance_id`, `seed_supplied` (bool) |
| `streams.stream.start_requested` / `.pause_requested` / `.resume_requested` / `.stop_requested` | member | set | `target_tps` (on start), prior `status` |
| `streams.stream.system_paused` | system | set | `reason` (`quota`,`idle`) — the PRD §7 contract row |
| `streams.chaos_policy.updated` | member | set | `modes_changed`, per-mode `enabled`/`rate` after |
| `streams.schema_upgrade.scheduled` | member | set | `subject`, `to_version`, `at` (Phase 10) |
| `chaos.answer_key.accessed` | admin / `answer_key:read` key | set | `stream_id`, query `mode` filter — instructors' access to ground truth is itself auditable (ADR-0017) |

---

## 11. Dashboards (Phase 11 deliverable; panel list frozen now)

| Dashboard | Panels |
|---|---|
| **SLO overview** | Three SLI gauges + 30-day burn-down per SLO; budget remaining; current burn rate |
| **Control plane** | Request rate / error rate / p50-p99 by route; auth failures; rate limiting; Celery queues + beat freshness |
| **Data plane** | Aggregate generation TPS (business vs CDC); tick duration + overruns; lease map (held/desired); checkpoint age; ledger append health |
| **Backbone & sinks** | Publish rate/failures; consumer lag per group/partition; buffer commit-lag heatmap; oldest-partition age; WS connections/frames/drops |
| **Chaos** | Injections by mode; late-buffer pending/overdue; re-emission outcomes |
| **Tenant drill-down** (console, per workspace — built on stream stats, not Prometheus) | Per-stream TPS, totals, per-type counts, last event, chaos counts |

---

## 12. Ownership boundaries

| Concern | Owner |
|---|---|
| Process topology, failure domains the SLOs measure | [system-architecture.md](system-architecture.md) |
| fly.toml metrics/health wiring, log shipping, secrets | [deployment-architecture.md](deployment-architecture.md) |
| Capacity numbers behind alert thresholds; the scaling staircase | [scaling-strategy.md](scaling-strategy.md) |
| StreamStats/Observation invariants (INV-OBS-*) and Audit invariants (INV-AUD-*) | [../03-domain/domain-model.md](../03-domain/domain-model.md) |
| Stats/answer-key endpoint shapes | [../05-interfaces/api-specification.md](../05-interfaces/api-specification.md) |
| Abuse-control responses to `AuthFailureSpike`-class signals | [../06-quality/security-architecture.md](../06-quality/security-architecture.md) |
| Tests binding SLIs, stats reconciliation, and audit completeness to CI gates | [../06-quality/testing-strategy.md](../06-quality/testing-strategy.md) |
