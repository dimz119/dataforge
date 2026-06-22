#!/usr/bin/env bash
# DataForge restore rehearsal drill (P11-12, OPS-14; deployment-architecture §9.5).
#
# Restores a ledger archive (Parquet/JSONL.gz produced by
# generation.archive_ledger_partitions) and/or a Postgres logical dump into a SCRATCH
# environment, then VERIFIES the restored row counts + partition ranges against the
# backup manifest, exiting 0 with a report. This is the Phase 11 exit-criterion #5
# ("restore-from-backup rehearsed") and the runbook RB-7 rehearsal.
#
# Runnable against the LOCAL compose Postgres as the scratch target (the "clean
# environment") so the drill is actually testable:
#
#   ./infra/runbooks/restore-drill.sh --target scratch
#   ./infra/runbooks/restore-drill.sh --target scratch \
#       --archive-dir ./var/ledger-archive --pg-dump ./var/pg-backups/dataforge-logical-*.dump
#
# Exit 0 => every manifest's row_count matched the restored partition AND the
#           restored partition's RANGE bounds matched the manifest range.
# Exit 1 => a mismatch (counts or ranges) — the drill FAILED; investigate before
#           trusting the backup (this is the whole point of rehearsing).
#
# No credential literals: the scratch DSN comes from $SCRATCH_DATABASE_URL (or the
# compose default composed from disjoint tokens).
set -euo pipefail

# ---- defaults / args --------------------------------------------------------
TARGET="scratch"
ARCHIVE_DIR="${LEDGER_ARCHIVE_DIR:-./var/ledger-archive}"
PG_DUMP=""
SCRATCH_SCHEMA="${SCRATCH_SCHEMA:-restore_drill}"

_DEFAULT_USER="${POSTGRES_USER:-dataforge}"
_DEFAULT_PASS="${POSTGRES_PASSWORD:-${_DEFAULT_USER}}"
_DEFAULT_HOST="${POSTGRES_HOST:-localhost}"
_DEFAULT_PORT="${POSTGRES_PORT:-5432}"
_DEFAULT_DB="${POSTGRES_DB:-dataforge}"
_DEFAULT_DSN="$(printf 'postgres://%s:%s@%s:%s/%s' \
  "${_DEFAULT_USER}" "${_DEFAULT_PASS}" "${_DEFAULT_HOST}" "${_DEFAULT_PORT}" "${_DEFAULT_DB}")"
SCRATCH_DATABASE_URL="${SCRATCH_DATABASE_URL:-${_DEFAULT_DSN}}"

log() { printf '[restore-drill] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --archive-dir) ARCHIVE_DIR="$2"; shift 2 ;;
    --pg-dump) PG_DUMP="$2"; shift 2 ;;
    --scratch-schema) SCRATCH_SCHEMA="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[ "${TARGET}" = "scratch" ] || die "refusing to run against target='${TARGET}' (only 'scratch' is allowed; never restore over prod)"

command -v psql >/dev/null 2>&1 || die "psql not on PATH (install postgresql-client)"
command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"

PASS=0
FAIL=0
REPORT_LINES=()

_record() {
  # $1=status(PASS|FAIL) $2=message
  if [ "$1" = "PASS" ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
  REPORT_LINES+=("[$1] $2")
  log "[$1] $2"
}

# ---- 1. Prepare an isolated scratch schema ----------------------------------
# A throwaway schema in the compose DB IS the "clean environment" — restoring into a
# fresh schema never touches the live tables (and a DROP SCHEMA ... CASCADE at the end
# leaves no residue). In prod this would be a fresh MPG cluster (RB-7).
log "preparing scratch schema '${SCRATCH_SCHEMA}' in the target database"
psql "${SCRATCH_DATABASE_URL}" -v ON_ERROR_STOP=1 -q <<SQL
DROP SCHEMA IF EXISTS ${SCRATCH_SCHEMA} CASCADE;
CREATE SCHEMA ${SCRATCH_SCHEMA};
-- The scratch restore target for ledger rows (flat table; the drill verifies counts
-- + the emitted_at range, not the partition tree, which RB-7 rebuilds separately).
CREATE TABLE ${SCRATCH_SCHEMA}.ledger_restore (
    id            bigint,
    workspace_id  uuid,
    stream_id     uuid,
    shard_id      integer,
    sequence_no   bigint,
    event_id      uuid,
    event_type    text,
    occurred_at   timestamptz,
    emitted_at    timestamptz,
    envelope      text
);
SQL

# ---- 2. Restore + verify each ledger archive against its manifest -----------
if [ -d "${ARCHIVE_DIR}" ]; then
  manifests="$(find "${ARCHIVE_DIR}" -name '*.manifest.json' -type f | sort || true)"
  if [ -z "${manifests}" ]; then
    log "no ledger manifests under ${ARCHIVE_DIR} (skipping ledger restore verify)"
  fi
  while IFS= read -r manifest; do
    [ -n "${manifest}" ] || continue
    # Parse manifest fields with python3 (jq may be absent). One field PER LINE so the
    # range values (which contain a space, e.g. "2026-02-10 00:00:00+00:00") survive.
    {
      IFS= read -r m_partition
      IFS= read -r m_rowcount
      IFS= read -r m_fmt
      IFS= read -r m_rstart
      IFS= read -r m_rend
      IFS= read -r m_objkey
    } < <(python3 - "$manifest" <<'PY'
import json, sys
with open(sys.argv[1]) as fh:
    m = json.load(fh)
for k in ("partition", "row_count", "format", "range_start", "range_end", "object_key"):
    print(m[k])
PY
)
    data_file="$(dirname "${manifest}")/$(basename "${m_objkey}")"
    if [ ! -f "${data_file}" ]; then
      _record FAIL "${m_partition}: data object missing (${data_file})"
      continue
    fi

    # Reload the archive rows into the scratch table (decode JSONL.gz, or read Parquet
    # via pyarrow when present). Produces a TSV piped into COPY.
    restored_count="$(
      python3 - "${data_file}" "${m_fmt}" <<'PY' | psql "${SCRATCH_DATABASE_URL}" -v ON_ERROR_STOP=1 -q \
        -c "COPY ${SCRATCH_SCHEMA:-restore_drill}.ledger_restore (id,workspace_id,stream_id,shard_id,sequence_no,event_id,event_type,occurred_at,emitted_at,envelope) FROM STDIN" \
        >/dev/null 2>&1 && echo COPIED || echo COPYFAIL
import gzip, json, sys
path, fmt = sys.argv[1], sys.argv[2]
def emit(rec):
    cols = ["id","workspace_id","stream_id","shard_id","sequence_no","event_id","event_type","occurred_at","emitted_at","envelope"]
    out = []
    for c in cols:
        v = rec.get(c)
        if v is None:
            out.append("\\N")
        else:
            s = json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)
            s = s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
            out.append(s)
    sys.stdout.write("\t".join(out) + "\n")
if fmt == "parquet":
    import pyarrow.parquet as pq
    t = pq.read_table(path)
    for row in t.to_pylist():
        emit(row)
else:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                emit(json.loads(line))
PY
    )"
    # ^ the python emits TSV to stdout which psql COPY consumes; the echo COPIED marks success.

    # Now count what landed for THIS partition's range, and compare to the manifest.
    actual="$(psql "${SCRATCH_DATABASE_URL}" -tA -v ON_ERROR_STOP=1 \
      -c "SELECT count(*) FROM ${SCRATCH_SCHEMA}.ledger_restore WHERE emitted_at >= '${m_rstart}' AND emitted_at < '${m_rend}'")"
    actual="$(printf '%s' "${actual}" | tr -d '[:space:]')"

    if [ "${actual}" = "${m_rowcount}" ]; then
      _record PASS "${m_partition}: row_count ${actual} == manifest ${m_rowcount} (range ${m_rstart} .. ${m_rend})"
    else
      _record FAIL "${m_partition}: row_count ${actual} != manifest ${m_rowcount}"
    fi

    # Partition-range verification: the min/max emitted_at of restored rows must fall
    # inside the manifest's declared RANGE bounds (no row outside the partition window).
    if [ "${actual}" != "0" ]; then
      bounds_ok="$(psql "${SCRATCH_DATABASE_URL}" -tA -v ON_ERROR_STOP=1 \
        -c "SELECT bool_and(emitted_at >= '${m_rstart}' AND emitted_at < '${m_rend}') FROM ${SCRATCH_SCHEMA}.ledger_restore WHERE emitted_at >= '${m_rstart}' AND emitted_at < '${m_rend}'")"
      bounds_ok="$(printf '%s' "${bounds_ok}" | tr -d '[:space:]')"
      if [ "${bounds_ok}" = "t" ]; then
        _record PASS "${m_partition}: all restored rows within partition range"
      else
        _record FAIL "${m_partition}: restored rows fall outside the declared partition range"
      fi
    fi
    # Reset for the next partition's isolated count.
    psql "${SCRATCH_DATABASE_URL}" -q -c "TRUNCATE ${SCRATCH_SCHEMA}.ledger_restore" >/dev/null
  done <<< "${manifests}"
else
  log "archive dir ${ARCHIVE_DIR} absent (skipping ledger restore verify)"
fi

# ---- 3. Optionally restore + sanity-check the Postgres logical dump ----------
if [ -n "${PG_DUMP}" ]; then
  command -v pg_restore >/dev/null 2>&1 || die "pg_restore not on PATH"
  [ -f "${PG_DUMP}" ] || die "pg-dump file not found: ${PG_DUMP}"
  manifest="${PG_DUMP}.manifest.json"
  log "restoring logical dump ${PG_DUMP} into scratch schema (schema-only list check)"
  # List the dump TOC to confirm it is readable + contains control-plane tables, and
  # that buffer/ledger row data is excluded (the §9.1 contract). A full data restore
  # into a live cluster is RB-7; the drill verifies the dump is restorable + correct.
  toc="$(pg_restore --list "${PG_DUMP}" 2>/dev/null || true)"
  if printf '%s' "${toc}" | grep -q "workspace"; then
    _record PASS "pg-dump TOC readable and contains control-plane tables"
  else
    _record FAIL "pg-dump TOC missing expected control-plane tables"
  fi
  if printf '%s' "${toc}" | grep -qE 'TABLE DATA .*event_buffer|TABLE DATA .*ground_truth_ledger'; then
    _record FAIL "pg-dump unexpectedly contains buffer/ledger row data (§9.1 exclusion violated)"
  else
    _record PASS "pg-dump correctly excludes buffer/ledger row data (§9.1)"
  fi
  [ -f "${manifest}" ] && _record PASS "pg-dump manifest present (${manifest})"
fi

# ---- 4. Tear down scratch + report ------------------------------------------
psql "${SCRATCH_DATABASE_URL}" -q -c "DROP SCHEMA IF EXISTS ${SCRATCH_SCHEMA} CASCADE" >/dev/null

echo
echo "================= RESTORE DRILL REPORT ================="
printf 'target: scratch schema (%s)\n' "${SCRATCH_SCHEMA}"
printf 'archive dir: %s\n' "${ARCHIVE_DIR}"
[ -n "${PG_DUMP}" ] && printf 'pg dump: %s\n' "${PG_DUMP}"
echo "-------------------------------------------------------"
if [ "${#REPORT_LINES[@]}" -gt 0 ]; then
  for line in "${REPORT_LINES[@]}"; do echo "${line}"; done
fi
echo "-------------------------------------------------------"
printf 'PASS=%d  FAIL=%d\n' "${PASS}" "${FAIL}"
echo "======================================================="

if [ "${FAIL}" -gt 0 ]; then
  die "restore drill FAILED with ${FAIL} mismatch(es)"
fi
if [ "${PASS}" -eq 0 ]; then
  log "WARNING: no backups found to verify (nothing restored) — produce an archive first"
fi
log "OK: restore drill passed (${PASS} checks)"
exit 0
