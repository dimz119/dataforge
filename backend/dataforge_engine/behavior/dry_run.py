"""Layer-3 dry-run host — the seeded, bounded execution of the *real* engine over
a candidate manifest (scenario-plugin-architecture §8.4; behavior-engine §1).

This is the third engine host (alongside the runner shard worker and golden replay):
it compiles the candidate manifest IR and drives it under the §8.4 sandbox — fixed
seed ``424242424242``, ``min(default, 1000)`` catalogs, backfill-style unpaced
execution, bounded by 30 s wall / 256 MiB RSS delta / 50,000 events / 1,000
completed session traversals (whichever first) — with a throwaway in-memory ledger
and a no-op pool store (no real DB writes, BE-T1). It observes realized behavior to
produce the §8.3 ``dry_run`` block and the MAN-D601..605 detections plus the
W-D610..612 warnings that static L1+L2 cannot see, closing the Phase-3 sequencing
window.

Determinism: with the fixed sandbox seed the *content* (and therefore the
event/payload/rate metrics) is reproducible; only ``est_eps_per_shard`` (a wall-time
throughput measurement) varies with the worker, which is exactly what MAN-D604
gates. The result is a pure data object the catalog merges into the persisted report.

Pure Python (BE-ENG-1): stdlib only, all I/O through the in-memory adapters here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .errors import CompileError, GenerationError, TraversalCapExceeded
from .ir import compile_manifest
from .observer import REMAINDER_INDEX
from .recorder import DryRunRecorder
from .shard import Shard, ShardConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dataforge_engine.envelope import InternalEnvelope

# ---------------------------------------------------------------------------
# The §8.4 sandbox bounds (fixed, published — comparable across runs/manifests).
# ---------------------------------------------------------------------------

SANDBOX_SEED = 424_242_424_242
MAX_CATALOG_PER_ENTITY = 1_000  # min(default, 1000) per entity (§8.4 Catalogs row)
WALL_BUDGET_SECONDS = 30.0
RSS_BUDGET_BYTES = 256 * 1024 * 1024  # 256 MiB RSS delta
EVENT_TARGET = 50_000  # stop at 50k events …
TRAVERSAL_TARGET = 1_000  # … or 1,000 completed session traversals
EPS_FLOOR = 1_000  # MAN-D604: est_eps_per_shard must be ≥ 1,000
PAYLOAD_CEIL_BYTES = 64 * 1024  # B-12 / MAN-D605: serialized payload ≤ 64 KiB
_PASS_SIZE = 500  # generate in 500-event passes so caps are checked between passes
_VIRTUAL_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _FastClock:
    """A deterministic, monotone wall clock for the dry run (1 µs/tick).

    The dry run must observe *its own* generation wall cost for ``est_eps_per_shard``,
    so the engine's ``emitted_at`` clock is decoupled from real time (this clock) and
    throughput is timed independently with :func:`time.perf_counter` around the drive
    loop. A pinned step keeps ``emitted_at`` reproducible.
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    def now(self) -> datetime:
        instant = _VIRTUAL_EPOCH + timedelta(microseconds=self._count)
        self._count += 1
        return instant


class _MemoryLedger:
    """A throwaway in-memory ledger sink — no DB writes (the dry run is sandboxed)."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def append(self, envelopes: Sequence[InternalEnvelope]) -> None:
        self.count += len(envelopes)


@dataclass
class DryRunResult:
    """The outcome of one L3 dry run — the §8.3 ``dry_run`` block + MAN-D/W findings.

    ``errors`` / ``warnings`` are ``(code, path, message, bound, actual)`` tuples the
    catalog facade renders into ``ValidationError`` / ``ValidationWarning`` objects.
    ``passed`` is ``True`` iff no MAN-D error fired.
    """

    metrics: dict[str, Any]
    errors: list[tuple[str, str, str, Any, Any]] = field(default_factory=list)
    warnings: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def est_eps_per_shard(self) -> int:
        return int(self.metrics.get("est_eps_per_shard", 0))


def _rss_bytes() -> int | None:
    """Best-effort current RSS in bytes (``None`` if unavailable on this platform)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is KiB on Linux, bytes on macOS/BSD. Normalise to bytes.
    import sys

    return usage if sys.platform == "darwin" else usage * 1024


def _payload_bytes(envelope: InternalEnvelope) -> int:
    """Serialized-payload byte size (B-12 / MAN-D605), Decimal-aware, compact."""
    import json
    from decimal import Decimal

    def _default(value: object) -> str:
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError(type(value).__name__)

    body = json.dumps(
        envelope["payload"], separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=_default,
    )
    return len(body.encode("utf-8"))


def run_dry_run(document: dict[str, Any]) -> DryRunResult:
    """Execute the §8.4 Layer-3 dry run on a Layer-1/2-valid candidate manifest.

    Returns a :class:`DryRunResult` carrying the persisted metrics, the MAN-D601..605
    errors, and the W-D610..612 warnings. Never raises for a *manifest* fault — a
    runtime fault the dry run is designed to catch (livelock, value realization) is
    converted to the matching MAN-D code. A :class:`CompileError` (a structural gap
    L1/L2 should have caught) surfaces as MAN-D603 at the document root.
    """
    try:
        ir = compile_manifest(document)
    except CompileError as exc:
        return DryRunResult(
            metrics={}, errors=[("MAN-D603", "", f"manifest failed to compile: {exc}", None, None)]
        )

    overrides = {name: min(default, MAX_CATALOG_PER_ENTITY) for name, default in ir.seeding.items()}
    config = ShardConfig(
        seed=SANDBOX_SEED, workspace_id="00000000-0000-0000-0000-000000000000",
        stream_id="00000000-0000-0000-0000-0000000000d3", shard_id=0,
        virtual_epoch=_VIRTUAL_EPOCH, mode="backfill", shard_count=1,
        catalog_overrides=overrides,
    )
    shard = Shard(ir, config, _FastClock())
    recorder = DryRunRecorder(ir)
    shard.interpreter.observer = recorder
    ledger = _MemoryLedger()
    return _drive(shard, ir, recorder, ledger)


def _drive(
    shard: Shard, ir: Any, recorder: DryRunRecorder, ledger: _MemoryLedger
) -> DryRunResult:
    """Drive the engine in bounded passes, enforcing every §8.4 cap deterministically."""
    rss_base = _rss_bytes()
    errors: list[tuple[str, str, str, Any, Any]] = []
    fault: str | None = None
    started = time.perf_counter()

    head = shard.seed()
    ledger.append(head)
    recorder.note_head(head)

    while True:
        elapsed = time.perf_counter() - started
        if elapsed >= WALL_BUDGET_SECONDS:
            fault = "MAN-D601-wall"
            break
        if rss_base is not None:
            rss_now = _rss_bytes()
            if rss_now is not None and rss_now - rss_base >= RSS_BUDGET_BYTES:
                fault = "MAN-D601-rss"
                break
        if ledger.count >= EVENT_TARGET or recorder.sessions_completed >= TRAVERSAL_TARGET:
            break
        if shard.heap.peek() is None:
            break  # heap drained before either completion target → too few events
        try:
            batch = shard.generate(_PASS_SIZE, _MAX_VIRTUAL_US)
        except TraversalCapExceeded:
            fault = "MAN-D602"
            break
        except GenerationError as exc:
            errors.append(
                ("MAN-D603", "", f"value realization failed in dry run: {exc}", None, None)
            )
            fault = "MAN-D603"
            break
        if not batch:
            # token headroom only (transaction would overflow the pass budget):
            # one transaction-sized pass clears it; empty again ⇒ no more work.
            try:
                batch = shard.generate(10, _MAX_VIRTUAL_US)
            except TraversalCapExceeded:
                fault = "MAN-D602"
                break
            if not batch:
                break
        ledger.append(batch)
        recorder.observe_batch(batch)

    generate_seconds = max(time.perf_counter() - started, 1e-9)
    return _finalize(ir, recorder, ledger, generate_seconds, fault, errors)


def _finalize(
    ir: Any, recorder: DryRunRecorder, ledger: _MemoryLedger,
    generate_seconds: float, fault: str | None,
    errors: list[tuple[str, str, str, Any, Any]],
) -> DryRunResult:
    """Compute the §8.3 metrics + remaining MAN-D detections and W-D warnings."""
    completed = recorder.sessions_completed
    business = recorder.business_events
    eps = int(ledger.count / generate_seconds) if generate_seconds > 0 else 0
    mean_eps = round(recorder.session_events / completed, 2) if completed else 0.0
    metrics: dict[str, Any] = {
        "events_generated": ledger.count,
        "traversals_completed": completed,
        "mean_events_per_session": mean_eps,
        "max_payload_bytes": recorder.max_payload,
        "p99_payload_bytes": recorder.p99_payload(),
        "est_eps_per_shard": eps,
        "realized_rates": recorder.realized_rates(),
        "visits_per_actor_day": 1.0,
        "business_events": business,
    }

    if fault == "MAN-D601-wall":
        errors.append(("MAN-D601", "", "30 s wall budget exhausted before reaching "
                       "50,000 events or 1,000 completed sessions", WALL_BUDGET_SECONDS, None))
    elif fault == "MAN-D601-rss":
        errors.append(("MAN-D601", "", "256 MiB RSS budget exhausted before either "
                       "completion target", RSS_BUDGET_BYTES, None))
    elif fault == "MAN-D602":
        errors.append(("MAN-D602", "", "a traversal hit the 10,000-transition hard cap "
                       "(B-13) — guard-induced livelock V205/V207 could not see", 10_000, None))

    reached = ledger.count >= EVENT_TARGET or completed >= TRAVERSAL_TARGET
    if fault is None and not reached:
        errors.append(("MAN-D601", "", "generation halted before reaching 50,000 events or "
                       "1,000 completed sessions (near-absorbing / too-few-events)",
                       EVENT_TARGET, ledger.count))

    # MAN-D605: any realized payload over the 64 KiB B-12 ceiling.
    if recorder.max_payload > PAYLOAD_CEIL_BYTES:
        errors.append(("MAN-D605", "", "a realized serialized payload exceeded 64 KiB (B-12)",
                       PAYLOAD_CEIL_BYTES, recorder.max_payload))

    # MAN-D604: throughput floor (only meaningful if generation itself succeeded).
    if not any(e[0] in ("MAN-D601", "MAN-D602", "MAN-D603") for e in errors) and eps < EPS_FLOOR:
        errors.append(("MAN-D604", "", f"est_eps_per_shard {eps} is below the 1,000 events/s "
                       "floor; the manifest cannot honor Pro's 1,000 TPS cap", EPS_FLOOR, eps))

    warnings = _warnings(ir, recorder)
    return DryRunResult(metrics=metrics, errors=errors, warnings=warnings)


def _warnings(ir: Any, recorder: DryRunRecorder) -> list[tuple[str, str, str]]:
    """W-D610 guard-starved transitions; W-D611 unemitted event types; W-D612 entities."""
    out: list[tuple[str, str, str]] = []
    for machine_name, machine in ir.machines.items():
        for state_name, state in machine.states.items():
            for idx, transition in enumerate(state.transitions):
                if transition.guard.is_empty:
                    continue
                selected, passed = recorder.guard_stats(machine_name, state_name, idx)
                if selected > 0 and passed == 0:
                    out.append((
                        "W-D610",
                        f"/state_machines/{machine_name}/states/{state_name}/transitions/{idx}",
                        f"guard never passed in dry run (0/{selected} selections); "
                        "transition is unreachable in practice",
                    ))
    for event_type in ir.event_types:
        if recorder.event_type_count(event_type) == 0:
            out.append((
                "W-D611", f"/event_types/{event_type}",
                "event type was never emitted in the dry run (unreachable)",
            ))
    for entity in ir.entity_order:
        if not recorder.entity_referenced(entity):
            out.append((
                "W-D612", f"/entities/{entity}",
                "entity was never referenced by any emitted event in the dry run",
            ))
    return out


# Re-export the remainder sentinel for the recorder + tests.
__all__ = [
    "EPS_FLOOR",
    "EVENT_TARGET",
    "PAYLOAD_CEIL_BYTES",
    "REMAINDER_INDEX",
    "SANDBOX_SEED",
    "TRAVERSAL_TARGET",
    "DryRunResult",
    "run_dry_run",
]

_MAX_VIRTUAL_US = (1 << 62) - 1
