"""Checkpoint / snapshot payload compression seam (database-schema §5.3-5.4).

The schema specifies *zstd*-compressed payloads. ``zstandard`` is not a
dependency yet (Phase 4 writes checkpoints/snapshots only at batch finalization,
off the hot streaming path), so this module compresses with stdlib ``zlib`` behind
a 1-byte codec tag. Phase 5 (the runner's periodic 30-s checkpoint path) swaps in
zstd by adding a ``CODEC_ZSTD`` branch — the tag makes the format
forward-compatible, and old ``zlib`` rows keep decompressing.

The tag is the first byte of the stored ``bytea``: ``0x01`` = zlib.
"""

from __future__ import annotations

import zlib

CODEC_ZLIB = 0x01
# Phase 5: CODEC_ZSTD = 0x02 once zstandard is added for the hot checkpoint path.

__all__ = ["compress", "decompress"]


def compress(data: bytes) -> bytes:
    """Compress ``data`` with the current codec, prefixing the 1-byte tag."""
    return bytes([CODEC_ZLIB]) + zlib.compress(data, level=6)


def decompress(blob: bytes) -> bytes:
    """Decompress a tagged blob produced by :func:`compress`."""
    if not blob:
        return b""
    tag, body = blob[0], blob[1:]
    if tag == CODEC_ZLIB:
        return zlib.decompress(body)
    raise ValueError(f"unknown compression codec tag {tag:#x}")
