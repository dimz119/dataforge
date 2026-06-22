# Runbook: Restart `ws` (WebSocket fan-out)

The `ws` process group serves the WebSocket delivery channel (ASGI/Channels). Two
machines in prod. Clients **reconnect automatically and resume from their cursor**, so
a restart causes a brief reconnect blip, not delivery loss.

## Symptom / when to use

- `ws` machines saturated/unhealthy (drives [WsConnectBurn.md](WsConnectBurn.md)).
- `df_ws_connections_active` near the per-machine ceiling with rising `timeout`/`error`.
- After a deploy.

## Diagnosis

1. `fly status -a $FLY_APP` / `docker compose -p dataforge ps ws`.
2. `df_ws_connect_total{result}` split (accepted vs error/timeout),
   `df_ws_connections_active`, `df_ws_fanout_lag_seconds` p95.
3. Channel-layer Redis health (pub-sub backend for fan-out).
4. `service=ws level=error` recent lines.

## Steps (rolling)

- **Prod:** restart one `ws` machine at a time:
  ```
  fly status -a $FLY_APP | awk '/ws/{print $1}' \
    | while read -r m; do fly machines restart "$m" -a "$FLY_APP"; sleep 10; done
  ```
  Connected clients on the restarted machine drop and reconnect to the peer, then
  resume from their last cursor (no missed events; the buffer holds them).
- **Local:** `docker compose -p dataforge restart ws`.

## Verification

- `accepted` ratio (`sli:slo3_ws_connect:ratio_rate1h`) returns above 99.5%.
- `df_ws_connections_active` redistributes across machines; `error`/`timeout` drop.
- A test client connects → authenticates → `subscribed` within 3s, and a tail receives
  fresh frames.
- Zero new ERROR lines for 5 minutes.
