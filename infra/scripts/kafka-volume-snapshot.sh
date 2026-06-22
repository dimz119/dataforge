#!/usr/bin/env bash
# DataForge Kafka volume snapshot (P11-11; deployment-architecture §9.4).
#
# Kafka holds seconds-to-hours of in-flight delivery transit on a single broker's
# volume (`kafkadata`). Loss tolerance is BOUNDED — events lost from the broker are
# lost *from delivery only*; they remain in the ground-truth ledger and streams
# resume from checkpoints (§9.4). The protection is daily volume snapshots with a
# 5-day retention, NOT zero-loss replication (that is the managed-Kafka migration
# trigger, §4).
#
# In prod this maps to `fly volumes snapshots create <kafkadata_volume>` (a Fly
# platform op). Locally it snapshots the compose `kafkadata` volume by tar'ing the
# broker's data dir into a timestamped archive that `restore-drill.sh` / RB-6 can
# reload, so the snapshot mechanism is rehearsable against the dev stack:
#
#   ./infra/scripts/kafka-volume-snapshot.sh                  # snapshot compose kafka
#   SNAPSHOT_DIR=/snaps ./infra/scripts/kafka-volume-snapshot.sh
#
# This is an ARTIFACT/rehearsal script: the prod path is the Fly snapshot API
# (commented below); the compose path proves the snapshot + retention logic.
set -euo pipefail

COMPOSE_PROJECT="${COMPOSE_PROJECT:-dataforge}"
KAFKA_SERVICE="${KAFKA_SERVICE:-kafka}"
KAFKA_DATA_DIR="${KAFKA_DATA_DIR:-/var/lib/kafka/data}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-./var/kafka-snapshots}"
RETENTION_DAYS="${KAFKA_SNAPSHOT_RETENTION_DAYS:-5}"
COMPOSE_FILE="${COMPOSE_FILE:-infra/compose/compose.yaml}"

log() { printf '[kafka-snapshot] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---- prod path (Fly), documented but not executed in dev --------------------
# On Fly the broker's volume is snapshotted by the platform; this script's prod
# invocation is:
#     fly volumes list -a "$FLY_APP" | awk '/kafkadata/{print $1}' \
#       | xargs -I{} fly volumes snapshots create {}
#   Fly retains 5 days of automatic snapshots by default; this aligns RETENTION_DAYS.
if [ "${KAFKA_SNAPSHOT_TARGET:-compose}" = "fly" ]; then
  command -v fly >/dev/null 2>&1 || die "fly CLI not on PATH (prod snapshot path)"
  : "${FLY_APP:?FLY_APP required for the fly snapshot path}"
  log "creating Fly volume snapshot(s) for kafkadata on app ${FLY_APP}"
  fly volumes list -a "${FLY_APP}" | awk '/kafkadata/{print $1}' \
    | xargs -r -I{} fly volumes snapshots create {} -a "${FLY_APP}"
  log "OK (fly): retention is managed by the platform (5 days default)"
  exit 0
fi

# ---- compose path (dev rehearsal) -------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not on PATH"

container="$(docker compose -p "${COMPOSE_PROJECT}" -f "${COMPOSE_FILE}" ps -q "${KAFKA_SERVICE}" 2>/dev/null || true)"
[ -n "${container}" ] || die "kafka service '${KAFKA_SERVICE}' not running in project '${COMPOSE_PROJECT}'"

mkdir -p "${SNAPSHOT_DIR}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
snap_file="${SNAPSHOT_DIR}/kafkadata-${stamp}.tar.gz"

log "snapshotting ${KAFKA_DATA_DIR} from ${KAFKA_SERVICE} -> ${snap_file}"
# Stream a tar of the broker data dir out of the container. Consistent enough for the
# bounded-loss posture (§9.4); a prod snapshot is volume-level and crash-consistent.
docker exec "${container}" tar -C "${KAFKA_DATA_DIR}" -cf - . | gzip > "${snap_file}"

size_bytes="$(wc -c < "${snap_file}" | tr -d ' ')"
manifest_file="${snap_file}.manifest.json"
cat > "${manifest_file}" <<JSON
{
  "kind": "kafka-volume-snapshot",
  "created_at": "${stamp}",
  "snapshot_file": "$(basename "${snap_file}")",
  "size_bytes": ${size_bytes},
  "source_dir": "${KAFKA_DATA_DIR}",
  "service": "${KAFKA_SERVICE}",
  "retention_days": ${RETENTION_DAYS},
  "loss_posture": "bounded-delivery-loss (ledger is ground truth, deployment-architecture 9.4)"
}
JSON
log "wrote snapshot (${size_bytes} bytes) + manifest"

if [ "${RETENTION_DAYS}" -gt 0 ]; then
  log "pruning snapshots older than ${RETENTION_DAYS} days in ${SNAPSHOT_DIR}"
  find "${SNAPSHOT_DIR}" -name 'kafkadata-*.tar.gz' -type f -mtime "+${RETENTION_DAYS}" -delete || true
  find "${SNAPSHOT_DIR}" -name 'kafkadata-*.tar.gz.manifest.json' -type f -mtime "+${RETENTION_DAYS}" -delete || true
fi

log "OK: ${snap_file}"
printf '%s\n' "${snap_file}"
