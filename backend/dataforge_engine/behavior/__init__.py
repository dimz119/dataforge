"""The generic behavior engine (behavior-engine D7).

A single generic interpreter that executes any compiled manifest IR over seeded
entity pools to produce the canonical event stream — zero scenario code (BE-T1),
state-first (BE-T2), deterministic by construction (BE-T3). Framework-free: stdlib
+ jsonschema only, no Django imports, all I/O through ``dataforge_engine.ports``;
the wall clock is injected (BE-ENG-1/2).

Public entrypoints (stable import paths the runner / dry-run worker / golden tests
depend on)::

    from dataforge_engine.behavior import (
        # the run/batch core
        Shard, ShardConfig,
        # IR compiler
        ManifestIR, compile_manifest, compile_manifest_cached, clear_ir_cache,
        # checkpoint codec (§9)
        encode_checkpoint, encode_to_json, restore_checkpoint, CODEC_VERSION,
        # Layer-3 dry-run host (§8.4) — the seeded, bounded validation execution
        run_dry_run, DryRunResult, SANDBOX_SEED, EPS_FLOOR,
        # errors
        EngineError, GenerationError, CompileError, CheckpointError,
        # identity + sequence
        StreamIdentity, SequenceCounter,
    )

The ports (``LedgerSink``, ``PoolStore``, ``WallClock``, ``RandomBitsSource``,
``SnapshotSink``) and the seed tree (``dataforge_engine.seeds``) are separate
modules so hosts wire I/O without importing the engine internals.
"""

from __future__ import annotations

from .checkpoint import (
    CODEC_VERSION,
    encode_checkpoint,
    encode_to_json,
    restore_checkpoint,
)
from .dry_run import (
    EPS_FLOOR,
    SANDBOX_SEED,
    DryRunResult,
    run_dry_run,
)
from .errors import CheckpointError, CompileError, EngineError, GenerationError
from .ir import (
    ManifestIR,
    clear_ir_cache,
    compile_manifest,
    compile_manifest_cached,
)
from .shard import Shard, ShardConfig
from .transaction import SequenceCounter, StreamIdentity

__all__ = [
    "CODEC_VERSION",
    "EPS_FLOOR",
    "SANDBOX_SEED",
    "CheckpointError",
    "CompileError",
    "DryRunResult",
    "EngineError",
    "GenerationError",
    "ManifestIR",
    "SequenceCounter",
    "Shard",
    "ShardConfig",
    "StreamIdentity",
    "clear_ir_cache",
    "compile_manifest",
    "compile_manifest_cached",
    "encode_checkpoint",
    "encode_to_json",
    "restore_checkpoint",
    "run_dry_run",
]
