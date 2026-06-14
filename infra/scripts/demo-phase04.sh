#!/usr/bin/env bash
# DataForge Phase 4 demo — steps 1-10 of
# specs/07-plan/phases/phase-04-generation-core-batch.md "Demo script".
#
# Exercises the Phase-4 surface end to end against the live compose stack:
#   instance create -> small sync dataset + determinism cmp -> event inspection ->
#   referential spot-check -> large async dataset + poll + download -> DuckDB E7 ->
#   golden + property gate runs -> L3 MAN-D602 (near-absorbing stay loop).
#
# API NOTE: the phase doc sketches `POST /api/v1/batches {seed,max_events}`; the
# shipped contract (api-spec §4.10, the hosting agent's build) is the *datasets*
# surface — `POST /api/v1/datasets` with `workspace_id`, `scenario_instance_id`,
# `name`, `simulated_days`, optional `seed` (a string), and a `?workspace_id=`
# query on the read/download routes. This script drives the shipped routes; the
# proven properties (determinism, 20-key envelope, referential validity, DuckDB
# E7) are identical.
#
# Auth ($ACCESS/$WS) is obtained the Phase-2 way (signup -> Mailpit verify ->
# login -> workspace create). Rerunnable from a clean stack (unique emails /
# names per run via a timestamp suffix); exits non-zero on any failure.
#
# Usage: infra/scripts/demo-phase04.sh [--no-up]
#   --no-up   skip `docker compose up -d --wait` (stack already healthy)

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/compose.yaml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
API="http://localhost:8000/api/v1"
MAILPIT="http://localhost:8025"
BACKEND="${REPO_ROOT}/backend"
WORKDIR="$(mktemp -d)"
FAILURES=0
DO_UP=1
RUN_SUFFIX="$(date +%s)"
# The pinned demo seed (testing-strategy §16.1 SEED_GOLD_A) so two runs match.
DEMO_SEED="271828182845"

for arg in "$@"; do
  case "${arg}" in
    --no-up) DO_UP=0 ;;
    *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
  esac
done

cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT

# mk_dataset / mk_big_dataset run inside `$(...)` command substitution (a
# subshell), so a plain `LAST_CODE=` assignment in them is lost to the parent.
# Route the HTTP status through a file the parent reads back.
LAST_CODE=""
CODE_FILE="${WORKDIR}/.last_code"
read_last_code() { LAST_CODE="$(cat "${CODE_FILE}" 2>/dev/null || echo '')"; }

pass() { printf 'PASS  step %s: %s\n' "$1" "$2"; }
fail() {
  printf 'FAIL  step %s: %s\n' "$1" "$2" >&2
  FAILURES=$((FAILURES + 1))
}
note() { printf '      %s\n' "$1"; }

require() {
  for cmd in curl jq python3; do
    command -v "${cmd}" >/dev/null 2>&1 || { echo "missing required tool: ${cmd}" >&2; exit 2; }
  done
  [[ -f "${REPO_ROOT}/infra/compose/.env" ]] || {
    echo "Missing infra/compose/.env — cp infra/compose/.env.example infra/compose/.env" >&2
    exit 2
  }
}

http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

mailpit_verify_token() {
  local email="$1" msg_id text
  msg_id="$(curl -fsS "${MAILPIT}/api/v1/search?query=to:${email}" 2>/dev/null \
    | jq -r '.messages // [] | sort_by(.Created) | last | .ID // empty' 2>/dev/null)"
  if [[ -z "${msg_id}" ]]; then
    msg_id="$(curl -fsS "${MAILPIT}/api/v1/messages?limit=200" 2>/dev/null \
      | jq -r --arg e "${email}" \
        '.messages // [] | map(select(any(.To[]?; .Address == $e)))
         | sort_by(.Created) | last | .ID // empty' 2>/dev/null)"
  fi
  [[ -n "${msg_id}" ]] || return 1
  text="$(curl -fsS "${MAILPIT}/api/v1/message/${msg_id}" | jq -r '.Text // ""')"
  printf '%s' "${text}" | grep -oE 'verify-email/[A-Za-z0-9._-]+' | head -n1 | cut -d/ -f2
}

# signup -> verify -> login -> create workspace; sets globals ACCESS and WS.
bootstrap_auth() {
  local email="ada+p4-${RUN_SUFFIX}@example.com" pw="correct-horse-battery" token code
  code="$(http_code -X POST "${API}/auth/signup" \
    -H 'Content-Type: application/json' -d "{\"email\":\"${email}\",\"password\":\"${pw}\"}")"
  [[ "${code}" == "201" ]] || { echo "signup ${email} -> ${code}" >&2; return 1; }
  token="$(mailpit_verify_token "${email}")"
  [[ -n "${token}" ]] || { echo "no verification token for ${email}" >&2; return 1; }
  code="$(http_code -X POST "${API}/auth/verify-email" \
    -H 'Content-Type: application/json' -d "{\"token\":\"${token}\"}")"
  [[ "${code}" == "200" ]] || { echo "verify ${email} -> ${code}" >&2; return 1; }
  ACCESS="$(curl -s -X POST "${API}/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${email}\",\"password\":\"${pw}\"}" | jq -r '.access_token // empty')"
  [[ -n "${ACCESS}" ]] || { echo "login got no access token" >&2; return 1; }
  WS="$(curl -s -X POST "${API}/workspaces" \
    -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
    -d "{\"name\":\"Phase4 Lab ${RUN_SUFFIX}\"}" | jq -r '.workspace_id // empty')"
  [[ -n "${WS}" ]] || { echo "workspace create returned no id" >&2; return 1; }
}

require
cd "${REPO_ROOT}"

# --- Step 1: bring the stack up; obtain $ACCESS/$WS ------------------------------
if [[ "${DO_UP}" -eq 1 ]]; then
  if "${COMPOSE[@]}" up -d --wait; then
    pass 1 "docker compose up -d --wait exited 0"
  else
    fail 1 "docker compose up -d --wait failed"
    echo "Aborting: remaining steps need a running stack." >&2
    exit 1
  fi
else
  pass 1 "stack bring-up skipped (--no-up)"
fi
if bootstrap_auth; then
  note "auth bootstrapped (phase-02 flow): ACCESS set, WS=${WS}"
else
  fail 1 "auth bootstrap failed — cannot continue"
  echo "Aborting." >&2
  exit 1
fi

# --- Step 2: create a scenario instance (small catalog overlay → fast sync) ------
# The overlay shrinks the seeding catalogs so the small batch is a sync 201.
INST_BODY="$(jq -nc --arg name "p4-inst-${RUN_SUFFIX}" '{
  name: $name, scenario_slug: "ecommerce", manifest_version: "1.0.0",
  configuration: { catalog_sizes: { users: 200, products: 50 } },
  default_seed: 271828182845
}')"
INST_RESP="$(curl -s -w '\n%{http_code}' -X POST \
  "${API}/workspaces/${WS}/scenario-instances" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' -d "${INST_BODY}")"
INST_CODE="$(printf '%s' "${INST_RESP}" | tail -n1)"
INST="$(printf '%s' "${INST_RESP}" | sed '$d' | jq -r '.scenario_instance_id // empty')"
if [[ "${INST_CODE}" == "201" && -n "${INST}" ]]; then
  pass 2 "scenario instance created (ecommerce 1.0.0): ${INST}"
else
  fail 2 "instance create expected 201 + id (got ${INST_CODE})"
  echo "Aborting: the dataset steps need an instance." >&2
  exit 1
fi

# --- Step 3: small sync dataset → ready, downloadable ----------------------------
mk_dataset() { # $1=name $2=simulated_days $3=compression  -> echoes dataset_id (+ sets LAST_CODE)
  local body resp
  body="$(jq -nc --arg ws "${WS}" --arg inst "${INST}" --arg name "$1" \
    --argjson days "$2" --arg seed "${DEMO_SEED}" --arg comp "$3" '{
      workspace_id: $ws, scenario_instance_id: $inst, name: $name,
      seed: $seed, simulated_days: $days, compression: $comp
    }')"
  resp="$(curl -s -w '\n%{http_code}' -X POST "${API}/datasets" \
    -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' -d "${body}")"
  printf '%s' "${resp}" | tail -n1 > "${CODE_FILE}"
  printf '%s' "${resp}" | sed '$d' | jq -r '.dataset_id // empty'
}

DS1="$(mk_dataset "sync-a-${RUN_SUFFIX}" 1 none)"
read_last_code
if [[ "${LAST_CODE}" == "201" && -n "${DS1}" ]]; then
  pass 3 "small sync dataset created → 201 ready (${DS1})"
else
  fail 3 "sync dataset expected 201 (got ${LAST_CODE})"
fi

# download dataset 1 (delivered-shape JSONL).
download() { # $1=dataset_id $2=outfile
  curl -s "${API}/datasets/$1/download?workspace_id=${WS}" \
    -H "Authorization: Bearer ${ACCESS}" -o "$2"
}
if [[ -n "${DS1}" ]]; then
  download "${DS1}" "${WORKDIR}/batch1.jsonl"
  N1="$(wc -l < "${WORKDIR}/batch1.jsonl" | tr -d ' ')"
  note "downloaded batch1.jsonl: ${N1} events"
fi

# --- Step 4: determinism — a second identical request yields identical bytes -----
# Same seed + same instance pin + same simulated window ⇒ byte-identical canonical
# content (INV-GEN-3). The download is canonical-serialized, so cmp is exact.
DS2="$(mk_dataset "sync-b-${RUN_SUFFIX}" 1 none)"
if [[ -n "${DS2}" ]]; then
  download "${DS2}" "${WORKDIR}/batch2.jsonl"
  if cmp -s "${WORKDIR}/batch1.jsonl" "${WORKDIR}/batch2.jsonl"; then
    pass 4 "determinism: two same-seed datasets are byte-identical (INV-GEN-3)"
  else
    fail 4 "determinism: same-seed datasets differ ($(diff <(head "${WORKDIR}/batch1.jsonl") \
      <(head "${WORKDIR}/batch2.jsonl") | head -1))"
  fi
else
  fail 4 "could not create the second dataset for the determinism check"
fi

# --- Step 5: inspect an event — 20 keys, schema_ref v1, shard 0, RFC3339 ---------
if [[ -s "${WORKDIR}/batch1.jsonl" ]]; then
  EV="$(head -1 "${WORKDIR}/batch1.jsonl")"
  KEYS="$(printf '%s' "${EV}" | jq 'keys | length')"
  SR_V="$(printf '%s' "${EV}" | jq -r '.schema_ref.version')"
  SHARD="$(printf '%s' "${EV}" | jq -r '.shard_id')"
  OCC="$(printf '%s' "${EV}" | jq -r '.occurred_at')"
  HAS_DF="$(printf '%s' "${EV}" | jq 'has("_df")')"
  if [[ "${KEYS}" == "20" && "${SR_V}" == "1" && "${SHARD}" == "0" \
        && "${HAS_DF}" == "false" && "${OCC}" =~ \.[0-9]{6}Z$ ]]; then
    pass 5 "event has 20 keys, schema_ref.version=1, shard_id=0, RFC3339 µs, no _df"
  else
    fail 5 "event inspection failed (keys=${KEYS} sr=${SR_V} shard=${SHARD} occ=${OCC} df=${HAS_DF})"
  fi
else
  fail 5 "no events to inspect"
fi

# --- Step 6: referential spot-check — order_placed.user_id resolves --------------
# Every order_placed payload.user_id appears as some prior event's actor_id (the
# user existed before the order). PROP-RI does this exhaustively; here we spot 3.
if [[ -s "${WORKDIR}/batch1.jsonl" ]]; then
  ORDER_USERS="$(jq -r 'select(.event_type=="order_placed") | .payload.user_id' \
    "${WORKDIR}/batch1.jsonl" | head -3)"
  ACTORS="$(jq -r '.actor_id // empty' "${WORKDIR}/batch1.jsonl" | sort -u)"
  MISS=0
  if [[ -n "${ORDER_USERS}" ]]; then
    while IFS= read -r uid; do
      [[ -z "${uid}" ]] && continue
      grep -qxF "${uid}" <<<"${ACTORS}" || MISS=$((MISS + 1))
    done <<<"${ORDER_USERS}"
    if [[ "${MISS}" -eq 0 ]]; then
      pass 6 "referential spot-check: 3 order_placed user_ids all resolve to a known user"
    else
      fail 6 "referential spot-check: ${MISS} order user_id(s) did not resolve"
    fi
  else
    note "no order_placed in the 1-day sync batch; the property suite covers this exhaustively"
    pass 6 "referential spot-check skipped (no orders in this small window); PROP-RI covers it"
  fi
fi

# --- Step 7: large async dataset → 202 + poll until ready → download -------------
# A larger estimate crosses the sync threshold (50k) → 202 on the exports queue.
# The small instance (200 users × 7 days × ~5 ≈ 7k) stays sync, so the async path
# needs a bigger catalog: a dedicated instance at ~3000 users (3000 × 7 × ~5 ≈
# 100k > 50k, well under the Free 1M cap). Poll the detail endpoint until ready.
BIG_BODY="$(jq -nc --arg name "p4-big-${RUN_SUFFIX}" '{
  name: $name, scenario_slug: "ecommerce", manifest_version: "1.0.0",
  configuration: { catalog_sizes: { users: 3000, products: 300 } },
  default_seed: 271828182845
}')"
BIG_INST="$(curl -s -X POST "${API}/workspaces/${WS}/scenario-instances" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d "${BIG_BODY}" | jq -r '.scenario_instance_id // empty')"
mk_big_dataset() { # $1=name $2=days  -> echoes dataset_id (+ sets LAST_CODE)
  local body resp
  body="$(jq -nc --arg ws "${WS}" --arg inst "${BIG_INST}" --arg name "$1" \
    --argjson days "$2" --arg seed "${DEMO_SEED}" '{
      workspace_id: $ws, scenario_instance_id: $inst, name: $name,
      seed: $seed, simulated_days: $days, compression: "none"
    }')"
  resp="$(curl -s -w '\n%{http_code}' -X POST "${API}/datasets" \
    -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' -d "${body}")"
  printf '%s' "${resp}" | tail -n1 > "${CODE_FILE}"
  printf '%s' "${resp}" | sed '$d' | jq -r '.dataset_id // empty'
}
DS3="$(mk_big_dataset "async-${RUN_SUFFIX}" 7)"
read_last_code
if [[ "${LAST_CODE}" == "202" || "${LAST_CODE}" == "201" ]] && [[ -n "${DS3}" ]]; then
  note "large dataset accepted (${LAST_CODE}); polling ${DS3} until ready"
  STATUS=""
  for _ in $(seq 1 60); do
    STATUS="$(curl -s "${API}/datasets/${DS3}?workspace_id=${WS}" \
      -H "Authorization: Bearer ${ACCESS}" | jq -r '.status // empty')"
    [[ "${STATUS}" == "ready" || "${STATUS}" == "failed" ]] && break
    sleep 2
  done
  if [[ "${STATUS}" == "ready" ]]; then
    download "${DS3}" "${WORKDIR}/batch_large.jsonl"
    NL="$(wc -l < "${WORKDIR}/batch_large.jsonl" | tr -d ' ')"
    pass 7 "large dataset reached ready and downloaded (${NL} events)"
  else
    fail 7 "large dataset did not reach ready (last status=${STATUS:-none})"
  fi
else
  fail 7 "large dataset create expected 202/201 (got ${LAST_CODE})"
fi

# --- Step 8: DuckDB exercise (OPS-11 / E7) on the largest available dataset ------
E7_INPUT="${WORKDIR}/batch_large.jsonl"
[[ -s "${E7_INPUT}" ]] || E7_INPUT="${WORKDIR}/batch1.jsonl"
if [[ -s "${E7_INPUT}" ]] && command -v uv >/dev/null 2>&1; then
  if (cd "${BACKEND}" && uv run python "${SCRIPT_DIR}/e7_duckdb_assert.py" "${E7_INPUT}"); then
    pass 8 "DuckDB E7: row count + 100% orders→users join + daily-revenue rows"
  else
    fail 8 "DuckDB E7 assertions failed on ${E7_INPUT}"
  fi
else
  fail 8 "no dataset to load into DuckDB (or uv unavailable)"
fi

# --- Step 9: gate suites — golden (byte-identity) + property (100k PR profile) ---
if command -v uv >/dev/null 2>&1; then
  if (cd "${BACKEND}" && uv run pytest -m golden -q >/dev/null 2>&1); then
    pass 9 "golden gate: pytest -m golden (GOLD-A byte-identity) green"
  else
    fail 9 "golden gate failed (pytest -m golden)"
  fi
  if (cd "${BACKEND}" && uv run pytest -m property -q >/dev/null 2>&1); then
    note "property gate: pytest -m property (PROP-RI 100k PR profile) green"
  else
    fail 9 "property gate failed (pytest -m property)"
  fi
  note "1M nightly profile (attended gate): cd backend && uv run pytest -m property_nightly -q"
else
  fail 9 "uv unavailable — cannot run the golden/property gates"
fi

# --- Step 10: L3 demo — POST a near-absorbing stay loop → MAN-D602 after dry run -
# Passes L1+L2 (static) but livelocks at runtime; the Layer-3 Celery dry run is the
# only stage that catches it. Poll the validation report until MAN-D602 appears.
LL_DOC="$(python3 "${SCRIPT_DIR}/phase04_fixtures.py" livelock)"
LL_SLUG="livelock_demo"
LL_RESP="$(curl -s -w '\n%{http_code}' -X POST "${API}/scenarios" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d "{\"workspace_id\":\"${WS}\",\"document\":${LL_DOC}}")"
LL_CODE="$(printf '%s' "${LL_RESP}" | tail -n1)"
if [[ "${LL_CODE}" == "201" || "${LL_CODE}" == "202" ]]; then
  note "livelock manifest accepted (${LL_CODE}); polling L3 validation for MAN-D602"
  D602=""
  for _ in $(seq 1 45); do
    REPORT="$(curl -s "${API}/scenarios/${LL_SLUG}/versions/1.0.0/validation?workspace_id=${WS}" \
      -H "Authorization: Bearer ${ACCESS}")"
    D602="$(printf '%s' "${REPORT}" | jq -r '(.errors // []) | map(.code) | index("MAN-D602")')"
    [[ "${D602}" != "null" && -n "${D602}" ]] && break
    sleep 2
  done
  if [[ "${D602}" != "null" && -n "${D602}" ]]; then
    pass 10 "L3 dry run flagged the near-absorbing stay loop with MAN-D602"
  else
    fail 10 "L3 validation did not report MAN-D602 (Celery validation worker running?)"
  fi
else
  fail 10 "livelock manifest POST expected 201/202 (got ${LL_CODE})"
fi

# --- Summary --------------------------------------------------------------------
echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "All Phase-4 demo steps passed."
  exit 0
fi
echo "${FAILURES} Phase-4 demo step(s) failed." >&2
exit 1
