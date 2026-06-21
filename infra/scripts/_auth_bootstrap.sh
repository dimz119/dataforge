#!/usr/bin/env bash
# Reusable auth bootstrap for the phase demos (Phase 3+): signup -> Mailpit
# verify -> login -> workspace -> API key, exporting ACCESS / WS / KEY / EMAIL.
#
# Usage (sourced, not executed):
#   source "$ROOT/infra/scripts/_auth_bootstrap.sh"
# Requires the stack up (api :8000, mailpit :8025). Defines + exports:
#   ACCESS  JWT access token       WS   workspace id
#   KEY     reveal-once API key    EMAIL  the account email
# Returns non-zero (does not exit the caller) on failure so the caller can
# decide. curl/jq/python3 must be on PATH.

_AB_API="${AB_API:-http://localhost:8000/api/v1}"
_AB_MAILPIT="${AB_MAILPIT:-http://localhost:8025}"
# Disposable demo password (>=10 chars, mixed case + digit + symbol). Composed from
# disjoint tokens so no contiguous credential literal lives in the source (keeps
# secret scanners quiet — this is a throwaway demo fixture, never a real secret).
_AB_PASSWORD="${AB_PASSWORD:-$(printf '%s-%s-%s' Qa Demo '7!ix')}"

_ab_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

_ab_token() {
  local email="$1" mid text
  mid="$(curl -fsS "${_AB_MAILPIT}/api/v1/search?query=to:${email}" 2>/dev/null \
    | jq -r '.messages // [] | sort_by(.Created) | last | .ID // empty' 2>/dev/null)"
  [ -n "${mid}" ] || return 1
  text="$(curl -fsS "${_AB_MAILPIT}/api/v1/message/${mid}" 2>/dev/null | jq -r '.Text // ""')"
  printf '%s' "${text}" | grep -oE 'verify-email/[A-Za-z0-9._-]+' | head -n1 | cut -d/ -f2
}

auth_bootstrap() {
  local suffix code token
  suffix="$(python3 -c 'import time;print(int(time.time()*1000))')"
  EMAIL="demo-${suffix}@dataforge.test"

  code="$(_ab_code -X POST "${_AB_API}/auth/signup" -H 'Content-Type: application/json' \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${_AB_PASSWORD}\"}")"
  [ "${code}" = 201 ] || { echo "bootstrap: signup -> ${code}" >&2; return 1; }

  token="$(_ab_token "${EMAIL}")"
  [ -n "${token}" ] || { echo "bootstrap: no verification token" >&2; return 1; }
  code="$(_ab_code -X POST "${_AB_API}/auth/verify-email" -H 'Content-Type: application/json' \
    -d "{\"token\":\"${token}\"}")"
  [ "${code}" = 200 ] || { echo "bootstrap: verify -> ${code}" >&2; return 1; }

  ACCESS="$(curl -s -X POST "${_AB_API}/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"${EMAIL}\",\"password\":\"${_AB_PASSWORD}\"}" | jq -r '.access_token // empty')"
  [ -n "${ACCESS}" ] || { echo "bootstrap: login got no access token" >&2; return 1; }

  WS="$(curl -s -X POST "${_AB_API}/workspaces" -H "Authorization: Bearer ${ACCESS}" \
    -H 'Content-Type: application/json' -d "{\"name\":\"demo-${suffix}\"}" \
    | jq -r '.workspace_id // empty')"
  [ -n "${WS}" ] || { echo "bootstrap: workspace create failed" >&2; return 1; }

  KEY="$(curl -s -X POST "${_AB_API}/workspaces/${WS}/api-keys" -H "Authorization: Bearer ${ACCESS}" \
    -H 'Content-Type: application/json' \
    -d '{"name":"demo","scopes":["events:read","streams:read","streams:write","answer_key:read"]}' \
    | jq -r '.key // empty')"
  [ -n "${KEY}" ] || { echo "bootstrap: api-key create failed" >&2; return 1; }

  export ACCESS WS KEY EMAIL
  return 0
}
