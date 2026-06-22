# Incident runbook: Restore rehearsal / Postgres restore (RB-7, OPS-14)

Restore ledger/buffer backups (and the control-plane logical dump) into a clean
environment and verify the restored data against the backup manifests. This is the
Phase 11 exit-criterion #5 ("restore-from-backup rehearsed") and the RB-7 procedure for
a real Postgres restore. The scripted, runnable rehearsal is
[`restore-drill.sh`](restore-drill.sh).

## Backups in scope (deployment-architecture §9)

| Backup | Produced by | Restored/verified by |
|---|---|---|
| Ledger Parquet/JSONL.gz archives + manifests | `generation.archive_ledger_partitions` (daily 02:00) | `restore-drill.sh` (counts + partition ranges vs manifest) |
| Postgres logical dump (control plane, excl. buffer/ledger data) | `infra/scripts/pg-backup.sh` (daily 01:00) | `restore-drill.sh --pg-dump …` (TOC + exclusion check); RB-7 full restore |
| MPG snapshots + PITR (physical, 7-day window) | managed Postgres | RB-7 (full cluster restore) |
| Kafka volume snapshots (5-day) | `infra/scripts/kafka-volume-snapshot.sh` | [kafka-volume-loss.md](kafka-volume-loss.md) (RB-6) |

## Quarterly game-day rehearsal (the drill)

Run the scripted drill against a scratch environment. Locally the compose Postgres is
the scratch target (a throwaway schema is the "clean environment"); in prod it is a
fresh MPG cluster.

```
# Local rehearsal against compose Postgres (scratch schema, auto torn down):
./infra/runbooks/restore-drill.sh --target scratch \
    --archive-dir ./var/ledger-archive \
    --pg-dump ./var/pg-backups/dataforge-logical-<stamp>.dump
```

The drill:
1. Creates an isolated scratch schema (never touches live tables; dropped at the end).
2. Restores each ledger archive object and asserts the restored **row count == manifest
   `row_count`** AND every restored row falls inside the manifest's **partition-range
   bounds** (`range_start` .. `range_end`).
3. If `--pg-dump` is given, confirms the dump TOC is readable, contains control-plane
   tables, and **correctly excludes** `event_buffer`/`ground_truth_ledger` row data
   (§9.1).
4. Prints a PASS/FAIL report and **exits 0 only if every check passed** (exit 1 on any
   mismatch — that is the signal the backup is untrustworthy).

It refuses any `--target` other than `scratch` (never restore over prod).

## RB-7 — Full Postgres restore (real incident)

1. Restore snapshot/PITR to a **new** MPG cluster (decision tree: choose the PITR point
   just before the corrupting event; RPO ≤ 5 min via WAL shipping).
2. `manage.py check --database` + migration-state verification on the restored cluster.
3. **Post-restore tenancy spot-audit** (cross-tenant suite, read-only) before reopening
   writes — confirm RLS isolation survived the restore.
4. Repoint `DATABASE_URL` via `fly secrets set` → rolling restart (RB-1 order).
5. Verify `readyz` + the staging smoke loop against prod (read-only checks). Target RTO
   60 min.

## Verification

- `restore-drill.sh` exits 0; the report shows PASS for every partition (counts +
  ranges) and the pg-dump checks.
- For a real RB-7: `manage.py check --database` clean, migration state matches, the
  tenancy spot-audit passes, `readyz` healthy, smoke loop green.
- RTO/RPO measured and any gaps filed (game-day output).
