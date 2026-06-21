"""The ordered chaos stage runner + mode registry (chaos-engine §2.2, P9-01).

The pipeline is the only consumer of the canonical stream and the only producer
onto delivery topics. It composes the enabled mode stages in the NORMATIVE,
non-configurable order (§2.2)::

    missing → duplicates → corrupted_values → nulls → schema_drift
            → out_of_order → late_arriving

Each stage is a PURE function over an envelope batch; the pipeline rebinds the
``StageContext.mode_config`` to the stage's policy slice before each call, so each
stage reads only its own sub-seed-derived decisions (§2.1). Disabled modes are
skipped (identity). Side effects are the two append-only ports on the context
(recorder / late_buffer) — INV-CHA-4.

Modes 5-6 (``schema_drift`` / ``out_of_order``) are registered; mode 7
(``late_arriving``) registers in later phase work via :data:`STAGE_REGISTRY`; the
pipeline already iterates them in order, so adding a stage is registry-only.

Pure Python (BE-ENG-1; engine purity import-linter contract). The ledger is NEVER
mutated by this pipeline (CHD-4/5) — content stages operate on clones.
"""

from __future__ import annotations

from typing import Final

from dataforge_engine.envelope import InternalEnvelope

from .context import Stage, StageContext
from .policy import CHAOS_MODES, ChaosMode, ChaosPolicy
from .stages import (
    CorruptedValuesStage,
    DuplicatesStage,
    LateArrivingStage,
    MissingStage,
    NullsStage,
    OutOfOrderStage,
    SchemaDriftStage,
)

# The mode registry: ChaosMode → the Stage factory. ``STAGE_ORDER`` is the §2.2
# normative order, which is the iteration order of the pipeline (NOT configurable).
# ``late_arriving`` is last/terminal (O-6): it extracts selected instances into the
# durable buffer, so nothing may run downstream of it.
STAGE_ORDER: Final[tuple[ChaosMode, ...]] = CHAOS_MODES

STAGE_REGISTRY: Final[dict[ChaosMode, type[Stage]]] = {
    "missing": MissingStage,
    "duplicates": DuplicatesStage,
    "corrupted_values": CorruptedValuesStage,
    "nulls": NullsStage,
    "schema_drift": SchemaDriftStage,
    "out_of_order": OutOfOrderStage,
    "late_arriving": LateArrivingStage,
}


class ChaosPipeline:
    """The ordered stage runner (§2.2).

    Constructed once per stream/tick context with the resolved
    :class:`ChaosPolicy`. ``transform`` applies the enabled stages in normative
    order, returning the post-chaos delivery batch. Records and late-buffer
    inserts happen as side effects on the supplied :class:`StageContext` ports —
    each written BEFORE the affected instance is published/suppressed (INV-CHA-4).
    """

    __slots__ = ("_policy", "_stages")

    def __init__(self, policy: ChaosPolicy) -> None:
        self._policy = policy
        # Instantiate exactly the registered stages, in normative order. Unknown /
        # not-yet-registered modes are skipped (they will appear once registered).
        self._stages: list[Stage] = [
            STAGE_REGISTRY[mode]()
            for mode in STAGE_ORDER
            if mode in STAGE_REGISTRY
        ]

    @property
    def stage_modes(self) -> list[str]:
        """The modes this pipeline will run, in normative order (for tests/metrics)."""
        return [stage.mode for stage in self._stages]

    def transform(
        self, batch: list[InternalEnvelope], ctx: StageContext
    ) -> list[InternalEnvelope]:
        """Apply the enabled stages in §2.2 order. Pure over ``(batch, policy)``.

        Disabled modes are identity (skipped inside each stage). The context's
        ``mode_config`` is rebound to each stage's policy slice before its call.
        """
        current = batch
        for stage in self._stages:
            ctx.mode_config = self._policy[stage.mode]  # type: ignore[literal-required]
            current = stage.process(current, ctx)
        return current


def normative_stage_order() -> tuple[ChaosMode, ...]:
    """The frozen §2.2 stage order — exposed for the structural order unit test."""
    return STAGE_ORDER
