#!/usr/bin/env bash
# DataForge Phase 7 demo — the "Demo script" of
# specs/07-plan/phases/phase-07-console-mvp.md (steps 1-9).
#
# Phase 7 is the CONSOLE MVP: the whole core loop is usable by a human in a
# browser. The phase-doc demo is a manual walk-through of the console UI; step 9
# is the HEADLESS PROOF — `npx playwright test e2e/core-loop.spec.ts` against the
# compose stack. This script automates the headless proof end to end and prints
# the manual steps (1-8) as the guided walk-through, with PASS/FAIL + evidence.
#
# It mirrors core-loop.spec.ts step for step (seed 4242 = SEED_E2E, so the data
# matches the PRD §2.2 instructor journey and the docs):
#   1 signup → verify via Mailpit            6 monitoring live tail (referential)
#   2 create workspace → GettingStartedPanel 7 TPS slider 10 → 200
#   3 scenario instance (+ MAN-V201 slider)  8 pause/resume/stop
#   4 reveal-once API key                    9 headless core-loop.spec.ts (PROOF)
#   5 create + start a stream
#
# COMPOSE-ONLY: the console + the full stack (api, ws, worker, runner,
# buffer-writer, web + the dev-only mailpit) must be up. The browser E2E is the
# binding proof; this script brings the stack up (unless --no-up), runs the smoke
# E2E lane (auth + core-loop) headless, and reports.
#
# Usage: infra/scripts/demo-phase07.sh [--no-up] [--keep-up] [--nightly]
#   --no-up    assume the stack is already healthy (skip compose up)
#   --keep-up  leave the stack running on exit (default tears nothing down —
#              compose is left as-is so you can `open http://localhost:5173`)
#   --nightly  also run the nightly E2E lane (keys/stream-control/live-tail/a11y)

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/compose.yaml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
FRONTEND_DIR="${REPO_ROOT}/frontend"
CONSOLE="http://localhost:5173"
API="http://localhost:8000"
MAILPIT="http://localhost:8025"
FAILURES=0
DO_UP=1
DO_NIGHTLY=0

for arg in "$@"; do
  case "${arg}" in
    --no-up) DO_UP=0 ;;
    --keep-up) : ;; # accepted; we never tear down by default
    --nightly) DO_NIGHTLY=1 ;;
    *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
  esac
done

pass() { echo "  PASS  [$1] $2"; }
fail() { echo "  FAIL  [$1] $2" >&2; FAILURES=$((FAILURES + 1)); }
note() { echo "        $1"; }
step() { echo; echo "=== $1 ==="; }

# --- bring up the stack ---------------------------------------------------------
step "Stack"
if [[ "${DO_UP}" -eq 1 ]]; then
  note "docker compose up -d --wait (pg, redis, kafka, api, ws, worker, runner, buffer-writer, web, mailpit)"
  if "${COMPOSE[@]}" up -d --wait; then
    pass "up" "compose stack is healthy"
  else
    fail "up" "compose stack failed to become healthy"
    "${COMPOSE[@]}" ps
    echo "Phase 7 demo: ABORTED (stack unhealthy)" >&2
    exit 1
  fi
else
  pass "up" "assuming the stack is already healthy (--no-up)"
fi

# --- readiness probes (demo command #2) -----------------------------------------
step "Readiness"
if curl -fsS "${API}/readyz" >/dev/null 2>&1; then
  pass "readyz" "api /readyz green (pg/redis/kafka probes)"
else
  fail "readyz" "api /readyz not green"
fi
if curl -fsS "${CONSOLE}/" >/dev/null 2>&1; then
  pass "web" "console served at ${CONSOLE}"
else
  fail "web" "console not reachable at ${CONSOLE}"
fi
if curl -fsS "${MAILPIT}/api/v1/messages" >/dev/null 2>&1; then
  pass "mailpit" "Mailpit REST API reachable at ${MAILPIT} (verification capture)"
else
  fail "mailpit" "Mailpit not reachable at ${MAILPIT}"
fi

# --- guided manual walk-through (demo steps 1-8) --------------------------------
# These steps are performed by a human in the browser; the headless E2E below
# automates the identical flow as the binding proof. We print the guide here so the
# script doubles as the demo runbook (open the console and follow along).
step "Guided console walk-through (steps 1-8 — open ${CONSOLE})"
cat <<GUIDE
  1. Sign up with a fresh email → open Mailpit (${MAILPIT}), click the verify link → land on the console.
  2. Create workspace 'demo-ws' → the dashboard shows the GettingStartedPanel.
  3. Scenarios → E-Commerce → Create instance (defaults). Drag one probability
     slider out of its allowed range → Save → the MAN-V201 error highlights the
     exact slider group (OverlayErrorMap). Fix and Save.
  4. API keys → Create key (events:read, streams:read, streams:write) → the
     reveal-once dialog shows the plaintext df_live_… ONCE → Copy → Close →
     the table shows only the masked prefix……last4 (reveal-once / INV-TEN-4).
  5. Streams → Create (instance, seed 4242, 10 TPS) → Start → StatusBadge
     starting → running in ≤ 60 s.
  6. Monitoring → live tail shows events; expand an order_placed row and confirm
     payload.user_id matches an earlier user_registered (the PRD activation moment).
  7. TPS slider 10 → 200 → observed TPS follows within 10 s; the tail stays
     responsive with the SamplingBadge active at 100+ TPS.
  8. Pause → badge 'paused', tail stops appending; Resume → appending continues;
     Stop → 'stopped'.
GUIDE
pass "guide" "manual walk-through printed (perform in the browser, or rely on the headless proof below)"

# --- step 9: the headless proof (binding) ---------------------------------------
# The exact phase-doc step 9: `npx playwright test e2e/core-loop.spec.ts`. The PR
# smoke lane is auth + core-loop (the Phase-7 exit gate); we run that lane here as
# the automated, reproducible proof that steps 1-8 work end to end in the UI.
step "Headless proof — PR smoke E2E lane (auth + core-loop)"
if ! command -v npx >/dev/null 2>&1; then
  fail "e2e" "npx not found — install Node 22 + run 'npm ci' in frontend/ first"
else
  note "cd frontend && npx playwright test --grep @smoke (auth.spec.ts + core-loop.spec.ts)"
  # Ensure the browser binary is present (no-op if already installed).
  ( cd "${FRONTEND_DIR}" && npx playwright install --with-deps chromium >/dev/null 2>&1 ) || true
  if ( cd "${FRONTEND_DIR}" && \
        E2E_BASE_URL="${CONSOLE}" E2E_API_URL="${API}" E2E_MAILPIT_URL="${MAILPIT}" \
        npx playwright test --grep @smoke ); then
    pass "e2e" "PR smoke E2E lane passed (core-loop.spec.ts is the Phase-7 exit gate)"
  else
    fail "e2e" "PR smoke E2E lane failed — see playwright-report/ + test-results/ for trace/video"
  fi
fi

# --- optional: the nightly lane (keys/stream-control/live-tail/a11y) ------------
if [[ "${DO_NIGHTLY}" -eq 1 ]]; then
  step "Nightly E2E lane (keys + stream-control + live-tail + a11y)"
  if ( cd "${FRONTEND_DIR}" && \
        E2E_BASE_URL="${CONSOLE}" E2E_API_URL="${API}" E2E_MAILPIT_URL="${MAILPIT}" \
        npx playwright test --grep @nightly ); then
    pass "nightly" "nightly E2E lane passed (reveal-once/revoke, lifecycle matrix, 100+ TPS tail, axe)"
  else
    fail "nightly" "nightly E2E lane failed — see playwright-report/ for evidence"
  fi
fi

# --- summary --------------------------------------------------------------------
echo
note "The stack is left running so you can explore: open ${CONSOLE}"
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "Phase 7 demo: ALL STEPS PASSED"
  exit 0
else
  echo "Phase 7 demo: ${FAILURES} STEP(S) FAILED" >&2
  exit 1
fi
