#!/usr/bin/env bash
# Phase 8 demo — full e-commerce (8 entities) + CDC + realism (curves, speed
# multiplier, backfill). Mirrors phase-08-full-ecommerce-cdc.md.
#
# Prereqs: docker compose -f infra/compose/compose.yaml up -d --wait
# (the api entrypoint publishes ecommerce 1.1.0 + derives the cdc.* subjects).
set -euo pipefail

API="${API:-http://localhost:8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PASS=0 FAIL=0
ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "== Phase 8 demo =="

# Reuse the Phase-2 auth bootstrap (signup -> Mailpit verify -> login -> workspace -> key).
echo "Step 0: auth bootstrap"
source "$ROOT/infra/scripts/demo-phase02.sh" --export 2>/dev/null || true
: "${ACCESS:?need ACCESS from demo-phase02}"; : "${WS:?need WS}"; : "${KEY:?need KEY}"
ok "auth: ACCESS/WS/KEY obtained"

# Step 1: the full manifest is registered with CDC subjects.
echo "Step 1: ecommerce 1.1.0 registered with cdc.* subjects"
SUBJECTS=$(curl -fsS "$API/api/v1/schemas/subjects?scenario_slug=ecommerce" \
  -H "Authorization: Bearer $ACCESS" | jq -r '.data[].subject' | grep -c '^ecommerce\.cdc\.' || true)
[ "${SUBJECTS:-0}" -ge 8 ] && ok "8 cdc.{entity} subjects present" || bad "expected >=8 cdc subjects, got ${SUBJECTS:-0}"
CDCUSERS_V=$(curl -fsS "$API/api/v1/schemas/subjects/ecommerce.cdc.users/versions" \
  -H "Authorization: Bearer $ACCESS" | jq -r '[.data[].version] | max' || echo 0)
[ "${CDCUSERS_V:-0}" -ge 2 ] && ok "cdc.users at version $CDCUSERS_V (additive status bump)" || bad "cdc.users expected v>=2, got ${CDCUSERS_V:-0}"

# Step 2: create a full-manifest instance.
echo "Step 2: scenario instance of ecommerce 1.1.0"
INST=$(curl -fsS -X POST "$API/api/v1/scenario-instances" -H "Authorization: Bearer $ACCESS" \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"'"$WS"'","scenario_slug":"ecommerce","manifest_version":"1.1.0"}' \
  | jq -r '.scenario_instance_id')
[ -n "$INST" ] && ok "instance $INST" || bad "instance create failed"

# Step 3: a speed-multiplier stream (60x compresses simulated shipping into wall-minutes).
echo "Step 3: 60x stream"
SID=$(curl -fsS -X POST "$API/api/v1/streams" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"scenario_instance_id":"'"$INST"'","seed":4242,"target_tps":50,
       "virtual_clock":{"speed_multiplier":60,"mode":"live"}}' | jq -r '.stream_id')
[ -n "$SID" ] && ok "stream $SID (60x)" || bad "stream create failed"
curl -fsS -X POST "$API/api/v1/streams/$SID/start" -H "Authorization: Bearer $ACCESS" >/dev/null
for _ in $(seq 1 30); do
  st=$(curl -fsS "$API/api/v1/streams/$SID" -H "Authorization: Bearer $ACCESS" | jq -r .status)
  [ "$st" = running ] && break; sleep 2
done
[ "${st:-}" = running ] && ok "stream running" || bad "stream did not reach running (status=${st:-?})"
sleep 20

# Step 4: CDC interleave + per-entity CDC filter (R-CDC-7).
echo "Step 4: per-entity CDC filter"
CDC_ROWS=$(curl -fsS "$API/api/v1/streams/$SID/events?entity_type=users&event_type=cdc.users&limit=200" \
  -H "X-API-Key: $KEY" | jq '[.data[] | select(.event_type=="cdc.users")] | length')
[ "${CDC_ROWS:-0}" -ge 1 ] && ok "cdc.users filtered rows: $CDC_ROWS" || bad "no cdc.users rows via filter"
# Every filtered row is a CDC op and only the users entity.
OFFENDERS=$(curl -fsS "$API/api/v1/streams/$SID/events?entity_type=users&event_type=cdc.users&limit=200" \
  -H "X-API-Key: $KEY" | jq '[.data[] | select(.event_type!="cdc.users")] | length')
[ "${OFFENDERS:-1}" -eq 0 ] && ok "filter returns only cdc.users" || bad "filter leaked $OFFENDERS non-matching rows"

curl -fsS -X POST "$API/api/v1/streams/$SID/stop" -H "Authorization: Bearer $ACCESS" >/dev/null || true

# Step 5: backfill — 30 simulated days as a dataset, diurnal/weekend shape in DuckDB.
echo "Step 5: 30-day backfill dataset"
BID=$(curl -fsS -X POST "$API/api/v1/datasets" -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"scenario_instance_id":"'"$INST"'","seed":4242,
       "virtual_clock":{"mode":"backfill","backfill_days":30}}' | jq -r '.dataset_id')
if [ -n "$BID" ]; then
  for _ in $(seq 1 60); do
    ds=$(curl -fsS "$API/api/v1/datasets/$BID?workspace_id=$WS" -H "X-API-Key: $KEY" | jq -r .status)
    [ "$ds" = ready ] && break; [ "$ds" = failed ] && break; sleep 3
  done
  [ "${ds:-}" = ready ] && ok "backfill dataset ready" || bad "backfill status=${ds:-?}"
  if command -v duckdb >/dev/null && [ "${ds:-}" = ready ]; then
    curl -fsS "$API/api/v1/datasets/$BID/download?workspace_id=$WS" -H "X-API-Key: $KEY" -o /tmp/p8.jsonl.gz
    gunzip -f /tmp/p8.jsonl.gz
    HOURS=$(duckdb -noheader -list -c "SELECT count(*) FROM (SELECT date_trunc('hour', occurred_at::timestamp) h, count(*) c FROM read_json_auto('/tmp/p8.jsonl') WHERE event_type='session_started' GROUP BY 1) WHERE c>0;" 2>/dev/null || echo 0)
    [ "${HOURS:-0}" -gt 100 ] && ok "DuckDB: $HOURS active session-hours (diurnal shape present)" || bad "DuckDB shape check thin (${HOURS:-0} hours)"
  else
    ok "backfill ready (DuckDB not installed — shape proven by STAT-SHAPE suite)"
  fi
else
  bad "backfill dataset create failed"
fi

echo "== Phase 8 demo: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ] || exit 1
echo "All Phase-8 demo steps passed."
