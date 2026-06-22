"""Ledger partition archive-to-Parquet (deployment-architecture §9.2-9.3; ADR-0017).

The ground-truth ledger keeps a **48 h hot window** in Postgres (database-schema
§5.5). Daily at 02:00 the archive job exports every ledger partition older than the
hot window to **Parquet on object storage** (Tigris/S3 in prod; a local directory
in dev), **verifies the exported row count matches the partition row count**, and
only then drops the partition (deployment-architecture §9.2). The verify-before-drop
ordering is the durability hinge: a partition is never dropped until its rows are
provably persisted in the cold tier (zero canonical loss within retention, SLO-2).

This module is the pure archive seam over one partition. It reads the partition rows
through the **owner DDL connection** (the same ``maintenance``-alias / owner-role
connection that drops the partition — §7.1), so the read sees every workspace's rows
in the partition (the archive is a platform-wide retention job, not a tenant read).

Parquet encoding is optional: when ``pyarrow`` is installed the export is true
columnar Parquet; otherwise the export falls back to a newline-delimited JSONL+zstd
sidecar with an identical manifest, so the job runs (and the verify-before-drop
contract holds) in the dev/compose environment where the heavy columnar dep is
absent. The on-disk format is recorded in the manifest so the restore drill can read
back either. Object-storage upload is a thin pluggable writer; the default writes to
``settings.DF_LEDGER_ARCHIVE_DIR`` (a local path in dev, a Tigris/S3 mount in prod).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from generation.infra import partitions

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)

__all__ = [
    "ArchiveManifest",
    "ArchiveResult",
    "archive_partition",
    "manifest_for",
    "write_manifest",
]

# The full ledger column set, in PK-stable order, exported verbatim so the restore
# drill can reload a partition row-for-row (database-schema §5.5).
_COLUMNS = (
    "id",
    "workspace_id",
    "stream_id",
    "shard_id",
    "sequence_no",
    "event_id",
    "event_type",
    "occurred_at",
    "emitted_at",
    "envelope",
)


@dataclass(frozen=True)
class ArchiveManifest:
    """The per-partition archive manifest (verified by the restore drill, OPS-14).

    ``row_count`` is the authoritative count the restore drill re-verifies; the
    partition-range bounds let the drill confirm it restored the correct window.
    """

    partition: str
    day: str  # YYYY-MM-DD (the partition's UTC day)
    row_count: int
    range_start: str  # inclusive RFC3339 lower bound (emitted_at)
    range_end: str  # exclusive RFC3339 upper bound (emitted_at)
    object_key: str  # the archived data object's storage key
    format: str  # "parquet" | "jsonl.gz"
    columns: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveResult:
    """The outcome of archiving + dropping one partition."""

    partition: str
    row_count: int
    archived: bool
    dropped: bool
    manifest_key: str
    object_key: str


def manifest_for(day: date, *, row_count: int, object_key: str, fmt: str) -> ArchiveManifest:
    """Build the manifest for ``day``'s partition (range = the partition's bounds)."""
    lo, hi = partitions._bounds(day)  # the exact attached RANGE bounds (§8.1)
    return ArchiveManifest(
        partition=partitions.partition_name(day),
        day=day.isoformat(),
        row_count=row_count,
        range_start=f"{lo}+00:00",
        range_end=f"{hi}+00:00",
        object_key=object_key,
        format=fmt,
        columns=_COLUMNS,
    )


def _archive_root(archive_dir: str) -> Path:
    root = Path(archive_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _object_key(day: date, fmt: str) -> str:
    ext = "parquet" if fmt == "parquet" else "jsonl.gz"
    return f"ledger/{day:%Y/%m}/{partitions.partition_name(day)}.{ext}"


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


def _row_count(cursor: Any, day: date) -> int:
    """Authoritative count of rows in ``day``'s partition (read on the DDL conn)."""
    name = partitions.partition_name(day)
    cursor.execute(f"SELECT count(*) FROM {name}")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def _fetch_rows(cursor: Any, day: date) -> list[dict[str, Any]]:
    name = partitions.partition_name(day)
    cols = ", ".join(_COLUMNS)
    cursor.execute(f"SELECT {cols} FROM {name} ORDER BY id")
    rows = cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record: dict[str, Any] = {}
        for col, value in zip(_COLUMNS, row, strict=True):
            # Normalise to JSON-safe scalars; timestamps/uuids stringify, the envelope
            # is already canonical JSON text (or a jsonb dict the driver decoded).
            if hasattr(value, "isoformat"):
                record[col] = value.isoformat()
            elif col == "envelope" and isinstance(value, dict):
                record[col] = value
            else:
                record[col] = str(value) if value is not None else None
        out.append(record)
    return out


def _write_parquet(rows: Sequence[dict[str, Any]], dest: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    # Envelope stays as canonical JSON text in the columnar file so the bytes survive
    # (jsonb would reorder keys — the S-2/S-3 byte-identity contract). Serialize any
    # decoded dict envelope back to compact JSON for the column.
    table_cols: dict[str, list[Any]] = {col: [] for col in _COLUMNS}
    for record in rows:
        for col in _COLUMNS:
            value = record.get(col)
            if col == "envelope" and isinstance(value, dict):
                value = json.dumps(value, separators=(",", ":"), sort_keys=False)
            table_cols[col].append(value)
    table = pa.table(table_cols)
    pq.write_table(table, dest, compression="zstd")


def _write_jsonl_gz(rows: Sequence[dict[str, Any]], dest: Path) -> None:
    # Dev/compose fallback: newline-delimited JSON, gzip-compressed. The manifest
    # records format="jsonl.gz" so the restore drill reads it back identically.
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        for record in rows:
            fh.write(json.dumps(record, separators=(",", ":"), sort_keys=True))
            fh.write("\n")


def _count_archived(dest: Path, fmt: str) -> int:
    """Re-read the just-written archive object and count its rows (verify step)."""
    if fmt == "parquet":
        import pyarrow.parquet as pq

        return int(pq.read_metadata(dest).num_rows)
    n = 0
    with gzip.open(dest, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


def write_manifest(manifest: ArchiveManifest, archive_dir: str) -> str:
    """Persist the manifest JSON next to the data object; return its key."""
    root = _archive_root(archive_dir)
    key = manifest.object_key.rsplit(".", 1)[0]
    # parquet|jsonl.gz both collapse to a single ".manifest.json" sidecar key.
    if key.endswith(".jsonl"):
        key = key[: -len(".jsonl")]
    manifest_key = f"{key}.manifest.json"
    dest = root / manifest_key
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(asdict(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return manifest_key


def archive_partition(
    cursor: Any, day: date, *, archive_dir: str, drop_after: bool = True
) -> ArchiveResult:
    """Archive ``day``'s ledger partition to object storage, verify, then drop.

    Steps (deployment-architecture §9.2):
      1. count the partition's rows (authoritative);
      2. export every row to a Parquet (or JSONL.gz fallback) object;
      3. re-read the object and assert its row count equals (1) — verify-before-drop;
      4. write the manifest (row_count + partition-range bounds, OPS-14);
      5. only on a verified match, detach+drop the partition (the only deletion).

    Raises ``ArchiveVerificationError`` if the exported count does not match; the
    partition is then left in place (the daily job re-attempts next run; the
    ``BufferRetentionStalled``/operational alert surfaces a stuck archive).
    """
    name = partitions.partition_name(day)
    total = _row_count(cursor, day)
    fmt = "parquet" if _has_pyarrow() else "jsonl.gz"
    object_key = _object_key(day, fmt)
    root = _archive_root(archive_dir)
    dest = root / object_key
    dest.parent.mkdir(parents=True, exist_ok=True)

    if total == 0:
        # An empty (or already-archived) partition: nothing to export, still safe to
        # drop. Write a zero-row manifest so the day is accounted for.
        manifest = manifest_for(day, row_count=0, object_key=object_key, fmt=fmt)
        manifest_key = write_manifest(manifest, archive_dir)
        dropped = False
        if drop_after:
            partitions.drop_partition(cursor, day)
            dropped = True
        logger.info("ledger.archive.empty", partition=name, dropped=dropped)
        return ArchiveResult(
            partition=name,
            row_count=0,
            archived=True,
            dropped=dropped,
            manifest_key=manifest_key,
            object_key=object_key,
        )

    rows = _fetch_rows(cursor, day)
    if fmt == "parquet":
        _write_parquet(rows, dest)
    else:
        _write_jsonl_gz(rows, dest)

    archived_count = _count_archived(dest, fmt)
    if archived_count != total:
        raise ArchiveVerificationError(
            f"{name}: archived {archived_count} rows but partition has {total} "
            "— refusing to drop (verify-before-drop, §9.2)"
        )

    manifest = manifest_for(day, row_count=total, object_key=object_key, fmt=fmt)
    manifest_key = write_manifest(manifest, archive_dir)

    dropped = False
    if drop_after:
        partitions.drop_partition(cursor, day)
        dropped = True
    logger.info(
        "ledger.archive.committed",
        partition=name,
        row_count=total,
        format=fmt,
        object_key=object_key,
        dropped=dropped,
    )
    return ArchiveResult(
        partition=name,
        row_count=total,
        archived=True,
        dropped=dropped,
        manifest_key=manifest_key,
        object_key=object_key,
    )


class ArchiveVerificationError(RuntimeError):
    """Raised when the exported Parquet row count does not match the partition."""
