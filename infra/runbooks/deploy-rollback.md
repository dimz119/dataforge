# Incident runbook: Deploy / rollback (RB-1, RB-2, RB-3)

Standard deploy and rollback for the single Fly app with `web`/`ws`/`worker`/`runner`
process groups built from one image (deployment-architecture §10). Migrations are
expand/contract and **N−1-compatible**, so a rollback is safe at any time.

## RB-1 — Standard deploy

1. Tag + approve the release.
2. Run the release command: **migrate + provision topics** (idempotent).
3. **Rolling restart, group order `worker → runner → ws → web`** (control plane first so
   new runner code never sees an older desired-state schema):
   ```
   fly deploy -a $FLY_APP --strategy rolling
   ```
   (Fly respects the per-group `release_command` + machine ordering; restart groups in
   order if doing it manually — see the per-component restart runbooks.)
4. **Verify:**
   - `readyz` healthy for every group.
   - Error-rate (`df_http_requests_total{status=~"5.."}`) and consumer-lag
     (`df_kafka_consumer_lag`) dashboards quiet for 15 minutes.
   - One scripted core-flow pass: `infra/scripts/prod-smoke.sh https://<prod-url>`.
   - Zero new ERROR lines during the window (SOAK assertion).

## RB-2 — Rollback (image)

Use when a deploy regressed (correlate the symptom — e.g.
[ApiAvailabilityBurn.md](ApiAvailabilityBurn.md) — with the release start in
`fly releases -a $FLY_APP`).

1. Identify the previous good digest: `fly releases -a $FLY_APP`.
2. Redeploy it:
   ```
   fly deploy -a $FLY_APP --image <previous-digest>
   ```
   Roll back **both apps** if Kafka config changed.
3. Safe at any time because migrations are N−1-compatible (RB-3) — the previous code runs
   against the new schema.
4. Verify as RB-1.

## RB-3 — Migration policy (why rollback is safe)

- **Expand/contract only.** Every migration must run against code version N−1: additive
  columns are nullable/defaulted; a drop happens **≥ 1 release after** code stops reading
  the column.
- **No down-migrations in prod.** A bad migration is rolled **forward** (a new fixing
  migration) or recovered via [restore-drill.md](restore-drill.md) / RB-7 — never reversed.
- CI runs the previous release's test suite against the new schema as the compatibility
  gate.

## Local rehearsal (compose)

The compose stack is the same nine services in the same shape as prod (§1), so the
rolling order and verification are rehearsable locally:
```
docker compose -p dataforge restart worker
docker compose -p dataforge restart runner
docker compose -p dataforge restart ws
docker compose -p dataforge restart web
```
Then run the core-flow smoke against `localhost`.

## Verification (deploy or rollback)

- All groups `readyz` healthy; metrics resume on the side-port / `/metrics` scrape.
- 5xx and consumer-lag dashboards quiet for 15 minutes.
- Core-flow smoke passes; zero new ERROR lines.
- The data plane never lost canonical events (per-shard `sequence_no` gapless across the
  rolling runner restart, D-5 / INV-GEN-5).
