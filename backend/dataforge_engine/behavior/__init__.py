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
        # generalized guard vocabulary (§5): relationship-existence, attribute
        # comparison, virtual-clock `within` window — all manifest-driven, no
        # scenario branching; failed guards fall through to the remainder (§6.2).
        Guard, Comparison, ExistsCondition, compile_guard, evaluate_guard,
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

from .background import BackgroundMutationDriver
from .checkpoint import (
    CODEC_VERSION,
    encode_checkpoint,
    encode_to_json,
    restore_checkpoint,
)
from .clock import VirtualClock
from .dry_run import (
    EPS_FLOOR,
    SANDBOX_SEED,
    DryRunResult,
    run_dry_run,
)
from .errors import CheckpointError, CompileError, EngineError, GenerationError
from .evaluate import evaluate_guard
from .intensity import IntensityCurve, compile_intensity
from .ir import (
    BackgroundMutationIR,
    Comparison,
    ExistsCondition,
    Guard,
    ManifestIR,
    clear_ir_cache,
    compile_guard,
    compile_manifest,
    compile_manifest_cached,
)
from .scheduler import ArrivalProcess, ArrivalState
from .shard import Shard, ShardConfig
from .transaction import Mutation, PoolTransaction, SequenceCounter, StreamIdentity

__all__ = [
    "CODEC_VERSION",
    "EPS_FLOOR",
    "SANDBOX_SEED",
    "ArrivalProcess",
    "ArrivalState",
    "BackgroundMutationDriver",
    "BackgroundMutationIR",
    "CheckpointError",
    "Comparison",
    "CompileError",
    "DryRunResult",
    "EngineError",
    "ExistsCondition",
    "GenerationError",
    "Guard",
    "IntensityCurve",
    "ManifestIR",
    "Mutation",
    "PoolTransaction",
    "SequenceCounter",
    "Shard",
    "ShardConfig",
    "StreamIdentity",
    "VirtualClock",
    "clear_ir_cache",
    "compile_guard",
    "compile_intensity",
    "compile_manifest",
    "compile_manifest_cached",
    "encode_checkpoint",
    "encode_to_json",
    "evaluate_guard",
    "restore_checkpoint",
    "run_dry_run",
]
