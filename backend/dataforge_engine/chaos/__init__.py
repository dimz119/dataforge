"""Chaos engine — the seeded, ordered, composable delivery-truth transform.

The chaos engine runs POST-ledger, PRE-publish (ADR-0009): it consumes the clean
canonical stream the behavior engine produced and transforms only what consumers
receive — the ledger is NEVER mutated (CHD-4/5), every delivered deviation maps to
exactly one :class:`InjectionRecord` (INV-CHA-4). It lives here, a SIBLING of
``dataforge_engine.behavior``, as a PURE engine module so it can be tested
deterministically and never contaminate the behavior engine (INV-CHA-1): the
behavior engine stays clean and chaos-unaware; chaos is a separate downstream
stage consuming the ``chaos`` sub-seed. The Django ``chaos`` app and the runner
wire it in via the recorder / late_buffer ports (it imports nothing framework).

Phase 9 (modes 1-4): the stage-pipeline framework (the §2.2 normative order, the
mode registry, the chaos sub-seed PRF), and modes ``missing`` / ``duplicates`` /
``corrupted_values`` / ``nulls``. Modes 5-7 (``schema_drift`` / ``out_of_order`` /
``late_arriving``) register into :data:`STAGE_REGISTRY` in later phase work.

Stable import paths::

    from dataforge_engine.chaos import (
        # pipeline (the stage-runner interface) + mode registry
        ChaosPipeline, STAGE_ORDER, STAGE_REGISTRY, normative_stage_order,
        # stage contract + ports
        Stage, StageContext, Recorder, LateBuffer, InMemoryRecorder,
        # policy document shape
        ChaosPolicy, ModeConfig, ChaosMode, CHAOS_MODES, OnStopPolicy,
        default_policy, RATE_MAX,
        # injection record + deterministic id
        InjectionRecord, deterministic_injection_id,
        # PRF (the chaos sub-seed draw)
        chaos_subseed, draw_u, draw_u64, digest, weighted_choice,
    )

Pure Python: zero Django / DRF / Celery / Channels / redis / confluent_kafka /
psycopg imports (BE-ENG-1; import-linter contract 2 is CI-blocking).
"""

from __future__ import annotations

from .context import (
    InMemoryRecorder,
    LateBuffer,
    Recorder,
    Stage,
    StageContext,
)
from .pipeline import (
    STAGE_ORDER,
    STAGE_REGISTRY,
    ChaosPipeline,
    normative_stage_order,
)
from .policy import (
    CHAOS_MODES,
    RATE_MAX,
    ChaosMode,
    ChaosPolicy,
    ModeConfig,
    OnStopPolicy,
    default_policy,
)
from .prf import (
    chaos_subseed,
    digest,
    draw_u,
    draw_u64,
    weighted_choice,
)
from .record import (
    InjectionRecord,
    deterministic_injection_id,
)

__all__ = [
    "CHAOS_MODES",
    "RATE_MAX",
    "STAGE_ORDER",
    "STAGE_REGISTRY",
    "ChaosMode",
    "ChaosPipeline",
    "ChaosPolicy",
    "InMemoryRecorder",
    "InjectionRecord",
    "LateBuffer",
    "ModeConfig",
    "OnStopPolicy",
    "Recorder",
    "Stage",
    "StageContext",
    "chaos_subseed",
    "default_policy",
    "deterministic_injection_id",
    "digest",
    "draw_u",
    "draw_u64",
    "normative_stage_order",
    "weighted_choice",
]
