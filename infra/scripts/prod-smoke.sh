#!/usr/bin/env bash
# DataForge post-deploy production smoke (P11-10, deployment-architecture §6.2 /
# phase-11 exit #3). Drives the FULL core product loop against ANY base URL and
# ASSERTS each step:
#
#   readyz -> signup -> verify-email -> login -> workspace -> API key ->
#   scenario instance -> stream create -> start -> pull events -> stop
#
# Reuses infra/scripts/_auth_bootstrap.sh for signup->verify->login->workspace->key
# (Mailpit-backed verification), so this script self-tests against the LOCAL
# compose stack out of the box. Against a real prod/staging URL the verification
# mail goes to a real provider (Postmark, §5), which a CI smoke cannot poll; in
# that case pass a pre-provisioned disposable account via --access/--ws/--key (or
# the env equivalents) and the script skips the signup/verify/login/workspace/key
# bootstrap and runs the stream loop directly.
#
# Usage:
#   infra/scripts/prod-smoke.sh [BASE_URL] [options]
#   infra/scripts/prod-smoke.sh                      # default http://localhost:8000 (compose self-test)
#   infra/scripts/prod-smoke.sh https://app.dataforge.dev
#   infra/scripts/prod-smoke.sh https://app.dataforge.dev \
#       --access "$JWT" --ws "$WS_ID" --key "$API_KEY"   # pre-provisioned account
#
# Options:
#   --access TOKEN   JWT access token (skip bootstrap; pair with --ws and --key)
#   --ws ID          workspace id (with --access)
#   --key KEY        API key with events:read + streams:* scopes (with --access)
#   --mailpit URL    Mailpit base for verification polling (default <BASE>:8025 host)
#   --scenario SLUG  scenario slug for the instance (default: ecommerce)
#   --no-cleanup     do not attempt to stop/clean the created stream
#
# Env equivalents: SMOKE_ACCESS, SMOKE_WS, SMOKE_KEY, AB_MAILPIT.
# Requires curl + jq + python3 on PATH. Exits non-zero on the first hard failure.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- arg parsing -------------------------------------------------------------
BASE_URL="http://localhost:8000"
ACCESS_IN="${SMOKE_ACCESS:-}"
WS_IN="${SMOKE_WS:-}"
KEY_IN="${SMOKE_KEY:-}"
MAILPIT_IN="${AB_MAILPIT:-}"
SCENARIO="ecommerce"
DO_CLEANUP=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --access)   ACCESS_IN="$2"; shift 2 ;;
    --ws)       WS_IN="$2"; shift 2 ;;
    --key)      KEY_IN="$2"; shift 2 ;;
    --mailpit)  MAILPIT_IN="$2"; shift 2 ;;
    --scenario) SCENARIO="$2"; shift 2 ;;
    --no-cleanup) DO_CLEANUP=0; shift ;;
    -h|--help)
      grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    http://*|https://*) BASE_URL="${1%/}"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

API="${BASE_URL%/}/api/v1"

# --- helpers -----------------------------------------------------------------
FAILURES=0
STREAM=""
ACCESS="${ACCESS_IN}"
WS="${WS_IN}"
KEY="${KEY_IN}"

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; FAILURES=$((FAILURES + 1)); }
note() { printf '      %s\n' "$1"; }
die()  { printf 'ABORT %s\n' "$1" >&2; exit 1; }

http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

require_tools() {
  for cmd in curl jq python3; do
    command -v "${cmd}" >/dev/null 2>&1 || die "missing required tool: ${cmd}"
  done
}

cleanup() {
  if [[ "${DO_CLEANUP}" -eq 1 && -n "${STREAM}" && -n "${ACCESS}" ]]; then
    curl -s -o /dev/null -X POST "${API}/streams/${STREAM}/stop" \
      -H "Authorization: Bearer ${ACCESS}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

require_tools
echo "DataForge prod-smoke against ${BASE_URL}"

# --- Step 0: readyz (all dependencies green) ---------------------------------
RZ_CODE="$(http_code "${BASE_URL%/}/readyz")"
if [[ "${RZ_CODE}" == "200" ]]; then
  pass "0  GET /readyz -> 200 (deps green)"
else
  fail "0  GET /readyz expected 200 (got ${RZ_CODE})"
  die "readyz not green — refusing to smoke an unhealthy target"
fi

# --- Steps 1-5: auth bootstrap (signup->verify->login->workspace->key) -------
if [[ -n "${ACCESS}" && -n "${WS}" && -n "${KEY}" ]]; then
  pass "1-5 using pre-provisioned account (--access/--ws/--key); bootstrap skipped"
  note "WS=${WS}"
else
  # Mailpit-backed bootstrap: point _auth_bootstrap.sh at this BASE_URL's API and
  # the appropriate Mailpit. Default Mailpit = same host, :8025 (compose self-test).
  host="$(printf '%s' "${BASE_URL}" | sed -E 's#^https?://##; s#[:/].*$##')"
  scheme="$(printf '%s' "${BASE_URL}" | sed -E 's#://.*##')"
  AB_API="${API}"
  AB_MAILPIT="${MAILPIT_IN:-${scheme}://${host}:8025}"
  export AB_API AB_MAILPIT
  # shellcheck source=infra/scripts/_auth_bootstrap.sh
  source "${SCRIPT_DIR}/_auth_bootstrap.sh"
  if auth_bootstrap; then
    pass "1-5 signup->verify->login->workspace->key bootstrapped"
    note "EMAIL=${EMAIL} WS=${WS}"
  else
    fail "1-5 auth bootstrap failed (Mailpit at ${AB_MAILPIT} reachable?)"
    die "cannot bootstrap an account; pass --access/--ws/--key for a real provider env"
  fi
fi

# --- Step 6: scenario instance (the stream's content source) -----------------
INST_BODY="$(jq -nc --arg name "smoke-$(date +%s)" --arg slug "${SCENARIO}" '{
  name: $name, scenario_slug: $slug, manifest_version: "1.0.0",
  configuration: { catalog_sizes: { users: 100, products: 30 } },
  default_seed: 271828 }')"
INST_RESP="$(curl -s -w '\n%{http_code}' -X POST "${API}/workspaces/${WS}/scenario-instances" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' -d "${INST_BODY}")"
INST_CODE="$(printf '%s' "${INST_RESP}" | tail -n1)"
INST="$(printf '%s' "${INST_RESP}" | sed '$d' | jq -r '.scenario_instance_id // empty')"
if [[ "${INST_CODE}" == "201" && -n "${INST}" ]]; then
  pass "6  scenario instance created (${SCENARIO} 1.0.0): ${INST}"
else
  fail "6  scenario instance expected 201 + id (got ${INST_CODE})"
  die "cannot create a stream without a scenario instance"
fi

# --- Step 7: create stream ---------------------------------------------------
STREAM_BODY="$(jq -nc --arg ws "${WS}" --arg inst "${INST}" '{
  workspace_id: $ws, scenario_instance_id: $inst, name: "smoke-stream",
  seed: "4242", target_tps: 10 }')"
STREAM_RESP="$(curl -s -w '\n%{http_code}' -X POST "${API}/streams" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' -d "${STREAM_BODY}")"
STREAM_CODE="$(printf '%s' "${STREAM_RESP}" | tail -n1)"
STREAM="$(printf '%s' "${STREAM_RESP}" | sed '$d' | jq -r '.stream_id // empty')"
if [[ "${STREAM_CODE}" == "201" && -n "${STREAM}" ]]; then
  pass "7  stream created (target_tps=10): ${STREAM}"
else
  fail "7  stream create expected 201 + id (got ${STREAM_CODE})"
  die "no stream to start"
fi

# --- Step 8: start; poll until running ---------------------------------------
START_CODE="$(http_code -X POST "${API}/streams/${STREAM}/start" -H "Authorization: Bearer ${ACCESS}")"
if [[ "${START_CODE}" == "200" ]]; then
  pass "8  POST /streams/{id}/start -> 200"
else
  fail "8  start expected 200 (got ${START_CODE})"
fi
RUNNING=0
for _ in $(seq 1 40); do
  ST="$(curl -s "${API}/streams/${STREAM}" -H "Authorization: Bearer ${ACCESS}" | jq -r '.status // empty')"
  [[ "${ST}" == "running" ]] && { RUNNING=1; break; }
  sleep 1
done
if [[ "${RUNNING}" -eq 1 ]]; then
  pass "8  stream reached status=running"
else
  fail "8  stream never reached running (last status=${ST:-?})"
fi

# --- Step 9: pull events over the API key (the delivery path) ----------------
EVCNT=0
for _ in $(seq 1 30); do
  PULL="$(curl -s -H "X-API-Key: ${KEY}" "${API}/streams/${STREAM}/events?from=earliest&limit=200")"
  EVCNT="$(printf '%s' "${PULL}" | jq -r '.data | length' 2>/dev/null || echo 0)"
  [[ "${EVCNT:-0}" -gt 0 ]] && break
  sleep 1
done
if [[ "${EVCNT:-0}" -gt 0 ]]; then
  pass "9  pulled ${EVCNT} events over X-API-Key (delivery live)"
  # cursor presence is part of the envelope contract ({data,next_cursor}).
  NEXT="$(printf '%s' "${PULL}" | jq -r '.next_cursor // empty')"
  [[ -n "${NEXT}" && "${NEXT}" != "null" ]] \
    && note "next_cursor present ({data,next_cursor} envelope OK)" \
    || note "no next_cursor (frontier may be at head)"
else
  fail "9  no events delivered within the warm-up window"
fi

# --- Step 10: stop; poll until stopped ---------------------------------------
STOP_CODE="$(http_code -X POST "${API}/streams/${STREAM}/stop" -H "Authorization: Bearer ${ACCESS}")"
if [[ "${STOP_CODE}" == "200" ]]; then
  pass "10 POST /streams/{id}/stop -> 200"
else
  fail "10 stop expected 200 (got ${STOP_CODE})"
fi
STOPPED=0
for _ in $(seq 1 20); do
  ST="$(curl -s "${API}/streams/${STREAM}" -H "Authorization: Bearer ${ACCESS}" | jq -r '.status // empty')"
  [[ "${ST}" == "stopped" ]] && { STOPPED=1; break; }
  sleep 1
done
if [[ "${STOPPED}" -eq 1 ]]; then
  pass "10 stream reached status=stopped"
  STREAM=""  # already stopped; cleanup trap has nothing to do
else
  note "stream status=${ST:-?} (stopping convergence) — cleanup trap will retry stop"
fi

# --- summary -----------------------------------------------------------------
echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "prod-smoke: ALL STEPS PASSED against ${BASE_URL}"
  exit 0
else
  echo "prod-smoke: ${FAILURES} step(s) FAILED against ${BASE_URL}" >&2
  exit 1
fi
