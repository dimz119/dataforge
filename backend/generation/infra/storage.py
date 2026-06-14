"""Dataset artifact storage — gzipped JSONL of delivered-shape envelopes
(api-spec §4.10.3).

The download file is **delivered-shape** envelopes (``_df`` stripped → the 20-key
envelope, INV-DEL-2) in ``(shard_id, sequence_no)`` order, one JSON object per
line, gzip-compressed (default) or plain NDJSON. Phase 11 moves these to object
storage with signed-URL redirects; until then the file lives on the local
filesystem under ``settings.DATASET_STORAGE_DIR`` and the API streams it directly.

The writer reads canonical internal envelopes from the ledger (the substrate,
behavior-engine §10), strips ``_df`` (event-model §5.2), and writes the JSONL. It
returns the artifact path, the event count, and the byte size for the dataset row.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings

from dataforge_engine.envelope import canonical_serialize_str, strip_internal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dataforge_engine.envelope import InternalEnvelope

__all__ = ["artifact_path", "delete_artifact", "write_jsonl"]


def _storage_dir() -> Path:
    raw = getattr(settings, "DATASET_STORAGE_DIR", None) or (settings.BASE_DIR / "var" / "datasets")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_path(dataset_id: str, *, compression: str) -> Path:
    """The on-disk artifact path for a dataset id (``.jsonl`` / ``.jsonl.gz``)."""
    suffix = ".jsonl.gz" if compression == "gzip" else ".jsonl"
    return _storage_dir() / f"{dataset_id}{suffix}"


def write_jsonl(
    *,
    dataset_id: str,
    envelopes: Iterable[InternalEnvelope],
    compression: str,
) -> tuple[Path, int, int]:
    """Write delivered-shape JSONL for a dataset; return (path, event_count, bytes).

    Strips ``_df`` from each internal envelope (→ 20-key delivered shape) and
    writes one compact JSON object per line. ``gzip`` uses a fixed ``mtime=0`` so
    the byte output is reproducible for an identical event sequence (INV-G-4).
    """
    path = artifact_path(dataset_id, compression=compression)
    count = 0
    if compression == "gzip":
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as gz:
            for env in envelopes:
                line = canonical_serialize_str(strip_internal(env)) + "\n"
                gz.write(line.encode("utf-8"))
                count += 1
    else:
        with path.open("w", encoding="utf-8") as fh:
            for env in envelopes:
                fh.write(canonical_serialize_str(strip_internal(env)) + "\n")
                count += 1
    return path, count, path.stat().st_size


def delete_artifact(path: str) -> None:
    """Remove a dataset artifact file (delete / expiry); idempotent."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
