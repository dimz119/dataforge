#!/usr/bin/env bash
# DataForge Phase 2 demo — steps 2-12 of
# specs/07-plan/phases/phase-02-identity-tenancy.md "Demo script".
# Prerequisite (step 1): docker compose -f infra/compose/compose.yaml up -d --wait
#   (run with --no-up to skip the bring-up if the stack is already healthy).
#
# Exercises the full Phase 2 surface end to end against the live stack:
#   signup -> mailpit token -> verify -> login (+ df_refresh cookie assertion) ->
#   workspace create -> API key reveal-once -> key-info probe -> revoke + <1s
#   stopwatch -> cross-tenant 404 probe -> guard pytest -> RLS pytest -> audit log.
# Prints PASS/FAIL per step with evidence; exits non-zero if any step fails.
# Rerunnable from a clean stack (unique emails per run via a timestamp suffix).

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/compose.yaml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
API="http://localhost:8000/api/v1"
MAILPIT="http://localhost:8025"
FAILURES=0
DO_UP=1
RUN_SUFFIX="$(date +%s)"

for arg in "$@"; do
  case "${arg}" in
    --no-up) DO_UP=0 ;;
    *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
  esac
done

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

# Status-only request: prints the HTTP code, swallows the body.
http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# Pull the newest verification token for an address out of Mailpit. Prefers the
# search endpoint; falls back to the full message list filtered by recipient so
# the demo is robust across Mailpit API revisions.
mailpit_verify_token() {
  local email="$1"
  local msg_id text
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
  # Link form: {CONSOLE_BASE_URL}/verify-email/<token>
  printf '%s' "${text}" | grep -oE 'verify-email/[A-Za-z0-9._-]+' | head -n1 | cut -d/ -f2
}

# Signup + verify + login for one account; echoes the access token on success.
signup_verify_login() {
  local email="$1" password="$2"
  local code token access
  code="$(http_code -X POST "${API}/auth/signup" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${email}\",\"password\":\"${password}\"}")"
  [[ "${code}" == "201" ]] || { echo "signup ${email} -> ${code}" >&2; return 1; }

  token="$(mailpit_verify_token "${email}")"
  [[ -n "${token}" ]] || { echo "no verification token for ${email}" >&2; return 1; }
  code="$(http_code -X POST "${API}/auth/verify-email" \
    -H 'Content-Type: application/json' -d "{\"token\":\"${token}\"}")"
  [[ "${code}" == "200" ]] || { echo "verify ${email} -> ${code}" >&2; return 1; }

  access="$(curl -s -X POST "${API}/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"${email}\",\"password\":\"${password}\"}" | jq -r '.access_token // empty')"
  [[ -n "${access}" ]] || { echo "login ${email} got no access token" >&2; return 1; }
  printf '%s' "${access}"
}

require
cd "${REPO_ROOT}"

ADA="ada+${RUN_SUFFIX}@example.com"
BOB="bob+${RUN_SUFFIX}@example.com"
PW="correct-horse-battery"

# --- Step 1/2: bring the stack up (skippable) -------------------------------
if [[ "${DO_UP}" -eq 1 ]]; then
  if "${COMPOSE[@]}" up -d --wait; then
    pass 2 "docker compose up -d --wait exited 0"
  else
    fail 2 "docker compose up -d --wait failed"
    echo "Aborting: remaining steps need a running stack." >&2
    exit 1
  fi
else
  pass 2 "stack bring-up skipped (--no-up)"
fi

# --- Step 2/3: signup Ada -> 201 --------------------------------------------
ADA_SIGNUP_CODE="$(http_code -X POST "${API}/auth/signup" \
  -H 'Content-Type: application/json' -d "{\"email\":\"${ADA}\",\"password\":\"${PW}\"}")"
if [[ "${ADA_SIGNUP_CODE}" == "201" ]]; then
  pass 2b "signup ${ADA} -> 201"
else
  fail 2b "signup ${ADA} -> ${ADA_SIGNUP_CODE} (expected 201)"
fi

# --- Step 3: fetch token from Mailpit, verify -> 200 ------------------------
ADA_TOKEN="$(mailpit_verify_token "${ADA}" || true)"
if [[ -n "${ADA_TOKEN}" ]]; then
  VERIFY_CODE="$(http_code -X POST "${API}/auth/verify-email" \
    -H 'Content-Type: application/json' -d "{\"token\":\"${ADA_TOKEN}\"}")"
  if [[ "${VERIFY_CODE}" == "200" ]]; then
    pass 3 "Mailpit token fetched; verify-email -> 200"
  else
    fail 3 "verify-email -> ${VERIFY_CODE} (expected 200)"
  fi
else
  fail 3 "no verification token found in Mailpit for ${ADA}"
fi

# --- Step 4: login -> access token in body, df_refresh ONLY in cookie -------
LOGIN_HEADERS="$(mktemp)"
LOGIN_BODY="$(curl -s -D "${LOGIN_HEADERS}" -X POST "${API}/auth/login" \
  -H 'Content-Type: application/json' -d "{\"email\":\"${ADA}\",\"password\":\"${PW}\"}")"
ACCESS="$(printf '%s' "${LOGIN_BODY}" | jq -r '.access_token // empty')"
SET_COOKIE="$(grep -i '^set-cookie:.*df_refresh' "${LOGIN_HEADERS}" || true)"
# SEC-AUTH-3: the refresh token is cookie-only and must NEVER appear in the body
# (neither the binding api-spec key `refresh_token` nor any `df_refresh` field).
BODY_HAS_REFRESH="$(printf '%s' "${LOGIN_BODY}" | jq -r 'has("refresh_token") or has("refresh") or (.df_refresh != null)')"
rm -f "${LOGIN_HEADERS}"
if [[ -n "${ACCESS}" && -n "${SET_COOKIE}" && "${BODY_HAS_REFRESH}" != "true" ]]; then
  pass 4 "login -> access in body; df_refresh set as HttpOnly cookie; not in body"
  note "Set-Cookie: $(printf '%s' "${SET_COOKIE}" | tr -d '\r' | sed 's/^[Ss]et-[Cc]ookie: //')"
else
  fail 4 "login cookie/body assertion (access=${ACCESS:+set}, cookie=${SET_COOKIE:+set}, body_refresh=${BODY_HAS_REFRESH})"
fi

# --- Step 5: create workspace -> workspace_id -------------------------------
# Workspace slug is globally unique (database-schema workspaces_slug_uq), so the
# name carries the run suffix too — exit criterion #1 requires the demo to be
# rerunnable, including against a stack that already holds prior runs' workspaces.
WS="$(curl -s -X POST "${API}/workspaces" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Ada Lab ${RUN_SUFFIX}\"}" | jq -r '.workspace_id // empty')"
if [[ -n "${WS}" ]]; then
  pass 5 "workspace created -> ${WS}"
else
  fail 5 "workspace create returned no workspace_id"
fi

# --- Step 6: create key (reveal-once); list shows only prefix...last4 -------
KEY_RESP="$(curl -s -X POST "${API}/workspaces/${WS}/api-keys" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d '{"name":"demo","scopes":["events:read","streams:write"]}')"
KEY="$(printf '%s' "${KEY_RESP}" | jq -r '.key // empty')"
KEY_ID="$(printf '%s' "${KEY_RESP}" | jq -r '.api_key_id // empty')"
LIST_HAS_PLAINTEXT="$(curl -s "${API}/workspaces/${WS}/api-keys" \
  -H "Authorization: Bearer ${ACCESS}" | jq -r '[.data[] | has("key")] | any')"
if [[ "${KEY}" == df_* && -n "${KEY_ID}" && "${LIST_HAS_PLAINTEXT}" == "false" ]]; then
  pass 6 "key minted reveal-once (${KEY:0:12}...); list hides plaintext"
else
  fail 6 "key reveal-once / list-hides-secret (key=${KEY:0:6}, id=${KEY_ID:+set}, list_plaintext=${LIST_HAS_PLAINTEXT})"
fi

# --- Step 7: use the key on key-info -> workspace echoes (X-API-Key only) ---
KI_WS="$(curl -s "${API}/auth/key-info" -H "X-API-Key: ${KEY}" | jq -r '.workspace_id // empty')"
if [[ "${KI_WS}" == "${WS}" ]]; then
  pass 7 "key-info via X-API-Key -> workspace_id == ${WS}"
else
  fail 7 "key-info workspace_id mismatch (got '${KI_WS}', expected '${WS}')"
fi

# --- Step 8: revoke -> 204; within 1s key-info -> 401 (stopwatch) -----------
REVOKE_CODE="$(http_code -X DELETE "${API}/workspaces/${WS}/api-keys/${KEY_ID}" \
  -H "Authorization: Bearer ${ACCESS}")"
START_NS="$(date +%s%N)"
AFTER_CODE="$(http_code "${API}/auth/key-info" -H "X-API-Key: ${KEY}")"
END_NS="$(date +%s%N)"
ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))
if [[ "${REVOKE_CODE}" == "204" && "${AFTER_CODE}" == "401" && "${ELAPSED_MS}" -lt 1000 ]]; then
  pass 8 "revoke -> 204; key-info -> 401 in ${ELAPSED_MS}ms (< 1000ms, SEC-KEY-5)"
else
  fail 8 "revoke/<1s reject (revoke=${REVOKE_CODE}, after=${AFTER_CODE}, elapsed=${ELAPSED_MS}ms)"
fi

# --- Step 9: cross-tenant probe — Bob requesting Ada's workspace -> 404 -----
ACCESS_BOB="$(signup_verify_login "${BOB}" "${PW}" || true)"
if [[ -n "${ACCESS_BOB}" ]]; then
  XT_CODE="$(http_code "${API}/workspaces/${WS}" -H "Authorization: Bearer ${ACCESS_BOB}")"
  if [[ "${XT_CODE}" == "404" ]]; then
    pass 9 "Bob GET Ada's workspace -> 404 (foreign masked, never 403; §3.3)"
  else
    fail 9 "cross-tenant probe expected 404, got ${XT_CODE}"
  fi
else
  fail 9 "could not establish Bob's session for the cross-tenant probe"
fi

# --- Step 10: guard pytest — planted canary fails check_tenancy -------------
if "${COMPOSE[@]}" exec -T api python -m pytest tests/guards/test_tenancy_guard.py -q \
  >/tmp/df-phase02-guard.log 2>&1; then
  pass 10 "guards/test_tenancy_guard.py passes (canaries red, controls green)"
else
  fail 10 "guard pytest failed — see /tmp/df-phase02-guard.log"
fi

# --- Step 11: RLS pytest — raw psycopg, foreign + unset GUC see 0 rows ------
# Runs under the Postgres-backed test settings so the RLS probes execute against
# the live compose Postgres (the default SQLite test settings would skip them).
if "${COMPOSE[@]}" exec -T -e DJANGO_SETTINGS_MODULE=config.settings.test_postgres api \
  python -m pytest tests/tenancy/test_rls_raw_sql.py -q -p no:cacheprovider \
  >/tmp/df-phase02-rls.log 2>&1; then
  if grep -q 'skipped' /tmp/df-phase02-rls.log && ! grep -q 'passed' /tmp/df-phase02-rls.log; then
    fail 11 "RLS pytest only SKIPPED (not Postgres) — see /tmp/df-phase02-rls.log"
  else
    pass 11 "tenancy/test_rls_raw_sql.py passes on Postgres (0 foreign / 0 unset rows)"
  fi
else
  fail 11 "RLS pytest failed — see /tmp/df-phase02-rls.log"
fi

# --- Step 12: audit log lists the Phase 2 minimum action set ----------------
ACTIONS="$(curl -s "${API}/workspaces/${WS}/audit-log" \
  -H "Authorization: Bearer ${ACCESS}" | jq -r '[.data[].action] | unique | join(",")')"
MISSING=()
for want in tenancy.workspace.created tenancy.api_key.created tenancy.api_key.revoked; do
  printf '%s' "${ACTIONS}" | grep -q "${want}" || MISSING+=("${want}")
done
# identity.user.registered is an account-level (NULL-workspace) row by design
# (INV-AUD-4): it is intentionally NOT served on the workspace audit-log surface.
if [[ "${#MISSING[@]}" -eq 0 ]]; then
  pass 12 "audit-log lists workspace.created, api_key.created, api_key.revoked"
  note "actions: ${ACTIONS}"
else
  fail 12 "audit-log missing action(s): ${MISSING[*]} (saw: ${ACTIONS})"
fi

echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "Phase 2 demo: ALL STEPS PASSED"
  exit 0
fi
echo "Phase 2 demo: ${FAILURES} step(s) FAILED" >&2
exit 1
