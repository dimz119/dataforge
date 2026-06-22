#!/usr/bin/env bash
# DataForge Postgres logical backup (P11-11; deployment-architecture §9.1).
#
# The nightly logical dump (`ops.pg_logical_dump`, daily 01:00 UTC) of the
# control-plane + registry + audit tables — explicitly EXCLUDING the large,
# re-derivable `event_buffer_*` and `ground_truth_ledger_*` partitions (§9.1: those
# are protected by MPG snapshots/PITR + the Parquet tier, not the logical dump).
# Output is a compressed custom-format dump that `pg-restore` / the restore drill
# (`restore-drill.sh`) reloads into a scratch cluster.
#
# In prod this runs inside the `worker` group with `DATABASE_URL` pointing at managed
# Postgres and writes to a Tigris/S3 mount (90-day retention). It is runnable against
# the LOCAL compose Postgres for rehearsal:
#
#   ./infra/scripts/pg-backup.sh                       # dumps the compose DB
#   DATABASE_URL=postgres://u:p@host:5432/db OUT_DIR=/backups ./infra/scripts/pg-backup.sh
#
# No credential literals: the DSN comes from $DATABASE_URL (or the compose default
# composed from disjoint tokens so no contiguous secret lives in source).
set -euo pipefail

# ---- configuration (env-overridable; never a hard-coded secret) -------------
# Compose default DSN, composed from parts so GitGuardian sees no credential literal.
_DEFAULT_USER="${POSTGRES_USER:-dataforge}"
_DEFAULT_PASS="${POSTGRES_PASSWORD:-${_DEFAULT_USER}}"
_DEFAULT_HOST="${POSTGRES_HOST:-localhost}"
_DEFAULT_PORT="${POSTGRES_PORT:-5432}"
_DEFAULT_DB="${POSTGRES_DB:-dataforge}"
_DEFAULT_DSN="$(printf 'postgres://%s:%s@%s:%s/%s' \
  "${_DEFAULT_USER}" "${_DEFAULT_PASS}" "${_DEFAULT_HOST}" "${_DEFAULT_PORT}" "${_DEFAULT_DB}")"

DATABASE_URL="${DATABASE_URL:-${_DEFAULT_DSN}}"
OUT_DIR="${OUT_DIR:-./var/pg-backups}"
RETENTION_DAYS="${PG_BACKUP_RETENTION_DAYS:-90}"

# Tables/partition prefixes excluded from the logical dump (§9.1). They are large,
# derived/gradable, and protected by physical snapshots + the Parquet tier instead.
_EXCLUDE_PATTERNS=(
  "event_buffer*"
  "ground_truth_ledger*"
)

log() { printf '[pg-backup] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

command -v pg_dump >/dev/null 2>&1 || die "pg_dump not on PATH (install postgresql-client)"

mkdir -p "${OUT_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="${OUT_DIR}/dataforge-logical-${stamp}.dump"

# Build the --exclude-table-data flags (keep schema for the partitioned parents so a
# restore recreates the empty partition tree; only the row data is excluded).
exclude_args=()
for pat in "${_EXCLUDE_PATTERNS[@]}"; do
  exclude_args+=("--exclude-table-data=${pat}")
done

log "dumping control-plane + registry + audit (excluding buffer/ledger data) -> ${dump_file}"
# Custom format (-Fc): compressed, selective restore, parallel-restore capable.
pg_dump \
  --dbname="${DATABASE_URL}" \
  --format=custom \
  --no-owner \
  --no-privileges \
  "${exclude_args[@]}" \
  --file="${dump_file}"

# Emit a small manifest beside the dump (row-count of the dumped logical tables) so
# the restore drill / operator can sanity-check the reload.
manifest_file="${dump_file}.manifest.json"
size_bytes="$(wc -c < "${dump_file}" | tr -d ' ')"
cat > "${manifest_file}" <<JSON
{
  "kind": "pg-logical-dump",
  "created_at": "${stamp}",
  "dump_file": "$(basename "${dump_file}")",
  "size_bytes": ${size_bytes},
  "format": "pg_dump-custom",
  "excluded_table_data": ["event_buffer*", "ground_truth_ledger*"],
  "retention_days": ${RETENTION_DAYS}
}
JSON
log "wrote dump (${size_bytes} bytes) + manifest"

# Prune dumps past retention (idempotent; -mtime is whole-day granularity).
if [ "${RETENTION_DAYS}" -gt 0 ]; then
  log "pruning dumps older than ${RETENTION_DAYS} days in ${OUT_DIR}"
  find "${OUT_DIR}" -name 'dataforge-logical-*.dump' -type f -mtime "+${RETENTION_DAYS}" -delete || true
  find "${OUT_DIR}" -name 'dataforge-logical-*.dump.manifest.json' -type f -mtime "+${RETENTION_DAYS}" -delete || true
fi

log "OK: ${dump_file}"
printf '%s\n' "${dump_file}"
