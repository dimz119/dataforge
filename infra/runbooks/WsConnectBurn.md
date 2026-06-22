# Runbook: WsConnectBurn (PAGE)

SLO-3 WS-connect error budget is burning: valid WebSocket handshakes are failing to
reach `subscribed` within 3s. Multiwindow burn alert (observability §8).

- **SLI:** `sli:slo3_ws_connect:ratio_rate{1h,6h}` — good = `accepted` /
  (`accepted` + `error` + `timeout`). `auth_failed` and `quota_rejected` are EXCLUDED
  (they are correct rejections, not failures).
- **Source metric:** `df_ws_connect_total{result=accepted|auth_failed|quota_rejected|error|timeout}`.
- **Target:** 99.5% over 30 days.

## Symptom

Clients open a WebSocket, authenticate, but the connection errors or times out before
the first `subscribed` frame. Live tails stall; the `df-ws` Grafana dashboard shows
rising `error`/`timeout` results and falling `accepted` ratio.

## Diagnosis

1. Split the failure mode:
   ```
   sum by (result) (rate(df_ws_connect_total[5m]))
   ```
   - `timeout` dominant → the `ws` group or its downstream (channel layer / Redis
     pub-sub / runner fan-out) is slow to deliver the first frame.
   - `error` dominant → handshake/subscription is throwing (look at ERROR logs).
   - `quota_rejected`/`auth_failed` rising → NOT this alert (excluded); those are
     correct caps (WS connections/workspace, or the WS-connect rate limit 10/min).
2. Check the `ws` group health: machines up, CPU, `df_ws_connections_active` near the
   per-machine ceiling? `fly status -a $FLY_APP` / `docker compose -p dataforge ps ws`.
3. Check the channel layer backend (Redis) — pub-sub latency/availability drives
   `df_ws_fanout_lag_seconds`.
4. Check fan-out lag: `histogram_quantile(0.95, ...df_ws_fanout_lag_seconds_bucket...)`
   — high lag means the runner→ws-pusher path is behind, so `subscribed`/first frame is
   late.
5. Inspect ERROR logs: `service=ws level=error`.

## Steps

- **`ws` machines saturated** → scale/restart the group
  ([restart-ws.md](restart-ws.md)). Clients auto-reconnect and resume from cursor
  (no delivery loss).
- **Channel-layer Redis degraded** → restart Redis; fan-out recovers when it returns.
- **Fan-out lag from the runner side** → correlate with
  [DeliveryFreshnessBurn.md](DeliveryFreshnessBurn.md) / runner health
  ([restart-runner.md](restart-runner.md)).
- **Regression after deploy** → roll back ([deploy-rollback.md](deploy-rollback.md)).

## Verification

- `accepted` ratio (`sli:slo3_ws_connect:ratio_rate1h`) climbs back above 99.5%.
- `error` + `timeout` rates return to baseline.
- A test client connects → authenticates → receives `subscribed` within 3s (spot probe).
- The burn windows drop below threshold and the alert resolves.
