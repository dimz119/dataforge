#!/usr/bin/env bash
# DataForge Phase 3 demo — steps 1-11 of
# specs/07-plan/phases/phase-03-manifest-registry-envelope.md "Demo script".
#
# Exercises the Phase-3 surface end to end against the live compose stack:
#   builtin sync (ecommerce 1.0.0 published at entrypoint) -> scenarios list ->
#   version validation_report=passed -> prob-sum 422 (MAN-V201) ->
#   escape-less-cycle 422 (MAN-V205) -> registry subjects -> version schema
#   (additionalProperties:false, all-required) -> BACKWARD_ADDITIVE compat
#   rejection naming the removed field -> envelope round-trip pytest ->
#   genericity grep empty.
#
# Auth ($ACCESS/$WS) is obtained the Phase-2 way: this script reuses the exact
# signup -> Mailpit verify -> login -> workspace-create flow that demo-phase02.sh
# established (the phase doc step 2). It is rerunnable from a clean stack (unique
# emails/workspace names per run via a timestamp suffix) and exits non-zero on any
# failure.
#
# Usage: infra/scripts/demo-phase03.sh [--no-up]
#   --no-up   skip `docker compose up -d --wait` (stack already healthy)

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

http_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# Pull the newest verification token for an address out of Mailpit (phase-02 helper).
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
  local email="ada+${RUN_SUFFIX}@example.com" pw="correct-horse-battery" token code
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
    -d "{\"name\":\"Phase3 Lab ${RUN_SUFFIX}\"}" | jq -r '.workspace_id // empty')"
  [[ -n "${WS}" ]] || { echo "workspace create returned no id" >&2; return 1; }
}

require
cd "${REPO_ROOT}"

# --- Step 1: bring the stack up; entrypoint sync_builtin_scenarios publishes -----
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
# Evidence: the builtin sync logged the publication (INV-CAT-1).
if "${COMPOSE[@]}" logs api 2>/dev/null | grep -qiE 'ecommerce.*1\.0\.0.*publish|publish.*ecommerce'; then
  note "entrypoint log shows ecommerce 1.0.0 published"
fi

# --- Step 2: auth — obtain $ACCESS and $WS (phase-02 flow) -----------------------
if bootstrap_auth; then
  pass 2 "auth bootstrapped (phase-02 flow): ACCESS set, WS=${WS}"
else
  fail 2 "auth bootstrap failed — cannot continue"
  echo "Aborting." >&2
  exit 1
fi

# --- Step 3: GET /scenarios lists ecommerce ------------------------------------
SLUGS="$(curl -s "${API}/scenarios" -H "Authorization: Bearer ${ACCESS}" \
  | jq -r '.data[].scenario_slug')"
if printf '%s\n' "${SLUGS}" | grep -qx 'ecommerce'; then
  pass 3 "GET /scenarios lists \"ecommerce\""
else
  fail 3 "GET /scenarios did not list ecommerce (saw: ${SLUGS//$'\n'/,})"
fi

# --- Step 4: version 1.0.0 validation_report status == passed -------------------
VSTATUS="$(curl -s "${API}/scenarios/ecommerce/versions/1.0.0/validation" \
  -H "Authorization: Bearer ${ACCESS}" | jq -r '.status // empty')"
if [[ "${VSTATUS}" == "passed" ]]; then
  pass 4 "ecommerce 1.0.0 validation_report.status == \"passed\""
else
  fail 4 "validation_report status expected \"passed\", got \"${VSTATUS}\""
fi

# --- Step 5: prob-sum violation POST -> 422 with MAN-V201 ----------------------
# Build a minimal workspace-visibility manifest with checkout probabilities = 1.15.
PROBSUM_DOC="$(python3 "${SCRIPT_DIR}/phase03_fixtures.py" prob_sum)"
RESP5="$(curl -s -w '\n%{http_code}' -X POST "${API}/scenarios" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d "{\"workspace_id\":\"${WS}\",\"document\":${PROBSUM_DOC}}")"
CODE5="$(printf '%s' "${RESP5}" | tail -n1)"
BODY5="$(printf '%s' "${RESP5}" | sed '$d')"
ERR5="$(printf '%s' "${BODY5}" | jq -c '.errors // [] | map(select(.code=="MAN-V201")) | .[0] // {}')"
HAS_V201="$(printf '%s' "${ERR5}" | jq -r '.code // empty')"
if [[ "${CODE5}" == "422" && "${HAS_V201}" == "MAN-V201" ]]; then
  pass 5 "prob-sum manifest -> 422 with MAN-V201"
  note "errors[0]: ${ERR5}"
else
  fail 5 "prob-sum expected 422+MAN-V201 (got ${CODE5}, err=${ERR5})"
fi

# --- Step 6: escape-less cycle POST -> 422 with MAN-V205 -----------------------
CYCLE_DOC="$(python3 "${SCRIPT_DIR}/phase03_fixtures.py" escape_less_cycle)"
RESP6="$(curl -s -w '\n%{http_code}' -X POST "${API}/scenarios" \
  -H "Authorization: Bearer ${ACCESS}" -H 'Content-Type: application/json' \
  -d "{\"workspace_id\":\"${WS}\",\"document\":${CYCLE_DOC}}")"
CODE6="$(printf '%s' "${RESP6}" | tail -n1)"
BODY6="$(printf '%s' "${RESP6}" | sed '$d')"
ERR6="$(printf '%s' "${BODY6}" | jq -c '.errors // [] | map(select(.code=="MAN-V205")) | .[0] // {}')"
HAS_V205="$(printf '%s' "${ERR6}" | jq -r '.code // empty')"
if [[ "${CODE6}" == "422" && "${HAS_V205}" == "MAN-V205" ]]; then
  pass 6 "escape-less cycle manifest -> 422 with MAN-V205 (names the SCC)"
  note "errors[0]: ${ERR6}"
else
  fail 6 "escape-less cycle expected 422+MAN-V205 (got ${CODE6}, err=${ERR6})"
fi

# --- Step 7: registry subjects for ecommerce ----------------------------------
SUBJECTS="$(curl -s "${API}/schemas?scenario_slug=ecommerce" \
  -H "Authorization: Bearer ${ACCESS}" | jq -r '.data[].subject' | sort)"
SUBJ_COUNT="$(printf '%s\n' "${SUBJECTS}" | grep -c .)"
if printf '%s\n' "${SUBJECTS}" | grep -qx 'ecommerce.order_placed' \
   && printf '%s\n' "${SUBJECTS}" | grep -qx 'ecommerce.cdc.orders' \
   && [[ "${SUBJ_COUNT}" -eq 13 ]]; then
  pass 7 "GET /schemas lists all 13 ecommerce subjects (business + cdc.*)"
  note "e.g. ecommerce.order_placed, ecommerce.cdc.orders"
else
  fail 7 "registry subjects incomplete (count=${SUBJ_COUNT}; expected 13)"
fi

# --- Step 8: version schema has additionalProperties:false + all-required -------
SCHEMA8="$(curl -s "${API}/schemas/ecommerce.order_placed/versions/1" \
  -H "Authorization: Bearer ${ACCESS}")"
# jq's `//` treats a literal `false` as "empty" and would coalesce away the
# correct additionalProperties:false value, so select the JSON-Schema object
# with `// {}` (object, never falsey) and read the field with `tostring`.
ADDL="$(printf '%s' "${SCHEMA8}" | jq -r '((.schema // .json_schema) // {}).additionalProperties | tostring')"
ALL_REQUIRED="$(printf '%s' "${SCHEMA8}" | jq -r '
  ((.schema // .json_schema) // {}) as $s
  | (($s.required // []) | sort) == (($s.properties // {} | keys) | sort)')"
VER8="$(printf '%s' "${SCHEMA8}" | jq -r '.version // empty')"
if [[ "${ADDL}" == "false" && "${ALL_REQUIRED}" == "true" && "${VER8}" == "1" ]]; then
  pass 8 "ecommerce.order_placed v1 schema: additionalProperties:false, every field required (R-DER-3)"
else
  fail 8 "version-1 schema gate (additionalProperties=${ADDL}, all_required=${ALL_REQUIRED}, version=${VER8})"
fi

# In-container pytest helpers. The DB-backed publish-path suites run under the
# Postgres test settings as the platform OWNER role — the production-faithful role
# for the manifest-publish transaction, which writes *global* (NULL-workspace)
# builtin rows that only the owner may INSERT (sync_builtin_scenarios runs as the
# owner via MIGRATE_DATABASE_URL at deploy time; database-schema §9.6). The runtime
# dataforge_app role can never write a global row — that backstop is asserted by the
# tenancy attack suite, not here. Pure-Python suites (tests/contract) are
# role-agnostic but ride the same settings for one consistent in-container lane.
PG_OWNER_URL="postgres://dataforge:dataforge@postgres:5432/dataforge"
pytest_owner() {
  "${COMPOSE[@]}" exec -T \
    -e DJANGO_SETTINGS_MODULE=config.settings.test_postgres \
    -e DATABASE_URL="${PG_OWNER_URL}" \
    -e MIGRATE_DATABASE_URL="${PG_OWNER_URL}" \
    -e REDIS_URL="redis://redis:6379/0" \
    api python -m pytest "$@"
}

# --- Step 9: BACKWARD_ADDITIVE compat rejects a field removal, naming it --------
# The registry read API is read-only (no /api/v1 :check endpoint by design —
# schema-registry §7). The BACKWARD_ADDITIVE gate runs at manifest publish
# (MAN-V501, "fail at the manifest"); the checker itself is unit-asserted to reject
# a field removal and name the field. We prove both with the in-container pytest.
if pytest_owner \
    tests/registry/test_integration_publish.py::test_backward_additive_rejects_field_removal_naming_it \
    tests/registry/test_compat.py::test_drop_field_is_rejected_c001 \
    -q >/tmp/df-phase03-compat.log 2>&1; then
  pass 9 "BACKWARD_ADDITIVE compat rejects a field removal, naming the removed field (REG-C001)"
else
  fail 9 "compat field-removal rejection pytest failed — see /tmp/df-phase03-compat.log"
fi

# --- Step 10: envelope round-trip + 20-key pin pytest --------------------------
if pytest_owner tests/contract -q \
    >/tmp/df-phase03-envelope.log 2>&1; then
  pass 10 "envelope round-trip: schema_ref stamped, canonical bytes stable, 20-key set pinned, artifact-valid"
else
  fail 10 "envelope contract pytest failed — see /tmp/df-phase03-envelope.log"
fi

# --- Step 11: genericity guard — grep empty + GUARD pytest green ----------------
GREP_HITS="$(cd "${REPO_ROOT}/backend" && grep -rn ecommerce . --include='*.py' \
  | grep -v catalog/builtin \
  | grep -vE '(^|/)tests?/|/test_|:[0-9]+:.*test_|ecommerce\.md' || true)"
if [[ -z "${GREP_HITS}" ]] && pytest_owner \
    tests/guards/test_no_ecommerce_in_python.py tests/guards/test_reference_manifest_no_hooks.py \
    -q >/tmp/df-phase03-guard.log 2>&1; then
  pass 11 "genericity grep empty; GUARD pytest green (no e-commerce logic in Python; reference manifest has zero hooks)"
else
  fail 11 "genericity guard failed (grep hits: ${GREP_HITS:-none}; see /tmp/df-phase03-guard.log)"
fi

echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "Phase 3 demo: ALL STEPS PASSED"
  exit 0
fi
echo "Phase 3 demo: ${FAILURES} step(s) FAILED" >&2
exit 1
