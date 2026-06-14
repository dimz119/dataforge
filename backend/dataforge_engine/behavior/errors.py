"""Typed errors raised by the behavior engine (behavior-engine §2, §4, §9).

The engine raises; the host (runner / dry-run worker / pytest) decides what to do.
It never sleeps, retries, or swallows — a raised error is a real defect or a
resource-bound breach the manifest validator should have caught.
"""

from __future__ import annotations


class EngineError(Exception):
    """Base class for every behavior-engine error."""


class GenerationError(EngineError):
    """A runtime generation fault (behavior-engine BE-A6/BE-E4): traversal hard
    cap exceeded, live non-terminal pool over cap, oversized event/checkpoint, or
    a ``derived.expr`` division-by-zero. Reachable only by a manifest the
    validator (MAN-V207 / L3 dry run) should have rejected — a defense line.

    The L3 dry-run host (scenario-plugin-architecture §8.4) distinguishes the
    traversal-hard-cap variant (:class:`TraversalCapExceeded` → MAN-D602) from a
    value-realization fault (a plain ``GenerationError`` → MAN-D603); the runner
    and golden hosts treat both identically.
    """


class TraversalCapExceeded(GenerationError):
    """The 10,000-transition traversal hard cap (B-13 / BE-A6) was crossed.

    A dedicated subclass so the L3 dry-run host can attribute it to MAN-D602
    (guard-induced livelock that V205/V207 could not see) rather than the generic
    value-realization MAN-D603. Production/golden hosts see only ``GenerationError``.
    """


class CompileError(EngineError):
    """The ManifestIR compiler could not bind a manifest (a structural gap the
    validator should have caught, or an unsupported grammar shape).
    """


class CheckpointError(EngineError):
    """The checkpoint codec could not encode/decode a blob, or restore found a
    ``pin_echo`` mismatch / unknown ``codec_version`` (behavior-engine §9.1/§9.3).
    """
