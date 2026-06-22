# Runbook: ApiAvailabilityBurn (PAGE)

SLO-1 control-plane availability error budget is burning fast. Multiwindow burn
alert (observability §8): pages when the 1h burn rate **and** the 5m burn rate both
exceed 14.4× (>2% of the 30-day budget/hour), or the 6h rate exceeds 6×.

- **SLI:** `sli:slo1_api_availability:ratio_rate{1h,6h}` (good = `/api/v1/*` requests
  with status < 500 answered within 30s; 4xx are GOOD).
- **Source metrics:** `df_http_requests_total{method,route,status}`,
  `df_http_request_duration_seconds{method,route}`.
- **Target:** 99.5% over 30 days.

## Symptom

The control-plane API is returning 5xx (or timing out > 30s) at a rate that will
exhaust the monthly error budget. Customers see failed signups, key/stream/workspace
commands, and console errors. The `df-slo-burn` Grafana dashboard shows the SLO-1
burn panels red.

## Diagnosis

1. Confirm scope — which routes and which status codes:
   ```
   topk(10, sum by (route, status) (rate(df_http_requests_total{status=~"5.."}[5m])))
   ```
   A single `route` spiking → that endpoint's handler/dependency. All routes → a
   shared dependency (Postgres, Redis) or the `web` group itself.
2. Check the `web` group health: `fly status -a $FLY_APP` (machines up?), `readyz` on
   each `web` machine. Locally: `docker compose -p dataforge ps web` + `curl
   localhost:8000/readyz`.
3. Check shared dependencies:
   - Postgres reachable + not saturated (connections, slow queries).
   - Redis reachable (auth/rate-limit/quota paths fail-open, but latency rises).
4. Check for a recent deploy (`fly releases -a $FLY_APP`) — correlate the burn start
   with a release; if so this is likely a regression → roll back (RB-2 /
   [deploy-rollback.md](deploy-rollback.md)).
5. Inspect ERROR logs for the burning route:
   ```
   service=web level=error  (filter by event=http.request.completed, status>=500)
   ```

## Steps

- **Recent deploy correlated** → roll back the image: see
  [deploy-rollback.md](deploy-rollback.md) (RB-2). Migrations are N−1-compatible so a
  rollback is safe at any time.
- **`web` machines unhealthy/crashed** → restart the group:
  see [restart-web.md](restart-web.md).
- **Postgres-attributed** → see the Postgres section of
  [restart-component.md](restart-component.md); if data loss/corruption, escalate to
  [restore-drill.md](restore-drill.md) / RB-7.
- **Redis-attributed** → restart Redis; the fail-open paths recover automatically once
  it is back.
- **Single dependent endpoint** → disable/feature-flag the offending path if possible,
  then fix forward.

## Verification

- `sli:slo1_api_availability:ratio_rate1h` climbs back above the 99.5% target line.
- 5xx rate (`rate(df_http_requests_total{status=~"5.."}[5m])`) returns to baseline.
- The 1h and 5m burn windows both drop below 14.4× and the alert resolves.
- One scripted core-flow pass succeeds (`infra/scripts/prod-smoke.sh` against the URL).
- Zero new ERROR lines for the affected route over a 15m quiet window.
