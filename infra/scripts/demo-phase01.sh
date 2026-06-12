#!/usr/bin/env bash
# DataForge Phase 1 demo — steps 2-7 and 10-11 of
# specs/07-plan/phases/phase-01-foundations.md "Demo script".
# Prerequisite (step 1): cp infra/compose/.env.example infra/compose/.env
#
# Prints PASS/FAIL per step; exits non-zero if any step fails.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/compose/compose.yaml"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")
FAILURES=0

pass() { printf 'PASS  step %s: %s\n' "$1" "$2"; }
fail() {
  printf 'FAIL  step %s: %s\n' "$1" "$2" >&2
  FAILURES=$((FAILURES + 1))
}

require_env() {
  if [[ ! -f "${REPO_ROOT}/infra/compose/.env" ]]; then
    echo "Missing infra/compose/.env — run step 1 first:" >&2
    echo "  cp infra/compose/.env.example infra/compose/.env" >&2
    exit 2
  fi
}

# All ten containers (nine platform services + dev-only mailpit) healthy.
assert_all_healthy() {
  "${COMPOSE[@]}" ps --format json | python3 -c '
import json, sys
rows = [json.loads(line) for line in sys.stdin if line.strip()]
expected = {"postgres", "redis", "kafka", "api", "ws", "worker",
            "runner", "buffer-writer", "web", "mailpit"}
seen = {r.get("Service"): r.get("Health", "") for r in rows}
missing = expected - set(seen)
unhealthy = {s: h for s, h in seen.items() if h != "healthy"}
if missing:
    print(f"missing services: {sorted(missing)}", file=sys.stderr); sys.exit(1)
if unhealthy:
    print(f"not healthy: {unhealthy}", file=sys.stderr); sys.exit(1)
print(f"{len(rows)} containers, all healthy")
'
}

assert_readyz_components_ok() {
  curl -fsS localhost:8000/readyz | python3 -c '
import json, sys
body = json.load(sys.stdin)
components = body.get("components", {})
bad = {c: components.get(c) for c in ("postgres", "redis", "kafka")
       if components.get(c) != "ok"}
if bad:
    print(f"readyz components not ok: {bad}", file=sys.stderr); sys.exit(1)
print("postgres/redis/kafka all ok")
'
}

require_env
cd "${REPO_ROOT}"

# --- Step 2: cold start, all services healthy -------------------------------
if "${COMPOSE[@]}" up -d --wait; then
  pass 2 "docker compose up -d --wait exited 0"
else
  fail 2 "docker compose up -d --wait failed"
  echo "Aborting: remaining steps need a running stack." >&2
  exit 1
fi

# --- Step 3: ps shows ten containers, every one healthy ---------------------
if assert_all_healthy; then
  pass 3 "ten containers (nine platform services + mailpit), all healthy"
else
  fail 3 "compose ps health assertion"
fi

# --- Step 4: api healthz 200; readyz green for pg/redis/kafka ---------------
if curl -fsS localhost:8000/healthz >/dev/null; then
  pass 4a "GET localhost:8000/healthz -> 200"
else
  fail 4a "GET localhost:8000/healthz"
fi
if assert_readyz_components_ok; then
  pass 4b "GET localhost:8000/readyz -> postgres/redis/kafka ok"
else
  fail 4b "readyz component map"
fi

# --- Step 5: runner + buffer-writer stubs alive (host 8090/8091) ------------
if curl -fsS localhost:8090/healthz >/dev/null; then
  pass 5a "runner stub healthz on localhost:8090"
else
  fail 5a "runner stub healthz on localhost:8090"
fi
if curl -fsS localhost:8091/healthz >/dev/null; then
  pass 5b "buffer-writer stub healthz on localhost:8091"
else
  fail 5b "buffer-writer stub healthz on localhost:8091"
fi

# --- Step 6: topic df.delivery.events.v1 exists with 12 partitions ----------
TOPIC_DESC="$("${COMPOSE[@]}" exec -T kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 --describe --topic df.delivery.events.v1 2>&1)"
if [[ "${TOPIC_DESC}" == *"PartitionCount: 12"* ]]; then
  pass 6 "topic df.delivery.events.v1 present with 12 partitions"
else
  fail 6 "topic df.delivery.events.v1 (expected PartitionCount: 12); got: ${TOPIC_DESC}"
fi

# --- Step 7: frontend shell renders ----------------------------------------
if curl -fsS localhost:5173/ >/dev/null; then
  pass 7 "frontend dev server responds on localhost:5173"
else
  fail 7 "frontend dev server on localhost:5173"
fi

# --- Step 10: folder-lint exits 0 -------------------------------------------
if python3 "${SCRIPT_DIR}/folder_lint.py"; then
  pass 10 "folder_lint.py exit 0"
else
  fail 10 "folder_lint.py"
fi

# --- Step 11: down/up persistence (fixed CLUSTER_ID: no Kafka reformat) -----
if "${COMPOSE[@]}" down && "${COMPOSE[@]}" up -d --wait && assert_all_healthy; then
  pass 11 "stack survives down/up with volumes intact (Kafka did not reformat)"
else
  fail 11 "down/up persistence check"
fi

echo
if [[ "${FAILURES}" -eq 0 ]]; then
  echo "Phase 1 demo: ALL STEPS PASSED"
  exit 0
fi
echo "Phase 1 demo: ${FAILURES} step(s) FAILED" >&2
exit 1
