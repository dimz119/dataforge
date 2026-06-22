# Runbook: Restart `web` (REST API)

The `web` process group serves `/api/v1/*` (WSGI) and hosts the `/metrics` endpoint on
the shared HTTP port. Two machines in prod; the load balancer reroutes during a
restart, so a rolling restart is **zero-downtime**.

## Symptom / when to use

- `web` machines unhealthy or crash-looping (drives
  [ApiAvailabilityBurn.md](ApiAvailabilityBurn.md)).
- After a deploy that needs a clean process start.
- Memory growth / stuck workers.

## Diagnosis

1. `fly status -a $FLY_APP` (prod) / `docker compose -p dataforge ps web` (local) —
   machine state + restart count.
2. `curl -fsS http://<web>/readyz` and `http://<web>/healthz` per machine.
3. `service=web level=error` recent ERROR lines; `df_http_requests_in_flight` (stuck
   requests) and `df_http_request_duration_seconds` p99.

## Steps (rolling, one machine at a time)

- **Prod:**
  ```
  fly status -a $FLY_APP | awk '/web/{print $1}' \
    | while read -r m; do fly machines restart "$m" -a "$FLY_APP"; \
        sleep 10; curl -fsS "http://$m.vm.$FLY_APP.internal:8000/readyz" || break; done
  ```
- **Local:** `docker compose -p dataforge restart web`.

Restart one machine, wait for it to pass `readyz`, then the next — never both at once
(keep capacity online).

## Verification

- Each `web` machine passes `readyz`.
- `df_http_requests_total{status=~"5.."}` rate returns to baseline;
  `df_http_requests_in_flight` is not pinned.
- `infra/scripts/prod-smoke.sh` (or a core-flow probe) passes.
- Zero new ERROR lines for 5 minutes.
