"""Stage contract, ``StageContext``, and the side-effect ports (chaos-engine §2.1).

Each chaos mode is one stage implementing a single interface::

    process(batch: list[InternalEnvelope], ctx: StageContext) -> list[InternalEnvelope]

A stage's output is a PURE function of ``(input batch, chaos_subseed, mode_config,
registry_view)`` — no wall-clock reads except stamping ``emitted_at`` at publish,
no I/O beyond the two append-only side effects (§2.1):

* ``ctx.recorder.record(InjectionRecord)`` — committed BEFORE the affected
  instance is emitted or suppressed (INV-CHA-4);
* ``ctx.late_buffer.insert(entry)`` — ``late_arriving`` only.

The recorder/late_buffer are PORTS: the pure engine declares the protocols; the
runner/Django ``chaos`` app supplies the Postgres-backed implementations. A pure
in-memory recorder is provided here for deterministic unit tests.

Pure Python (BE-ENG-1).
"""

from __future__ import annotations

from typing import Protocol

from dataforge_engine.envelope import InternalEnvelope

from .policy import ModeConfig
from .record import InjectionRecord


class Recorder(Protocol):
    """The append-only answer-key sink (§7.1). ``record`` must complete (commit)
    before the affected instance is published, buffered, or suppressed (INV-CHA-4).
    """

    def record(self, injection: InjectionRecord) -> None:
        """Persist one :class:`InjectionRecord` (idempotent on ``injection_id``)."""
        ...


class LateBuffer(Protocol):
    """The durable late-arrival buffer port (§6). Only ``late_arriving`` writes to
    it; modes 1-4 never touch it (the framework holds it for later phases).
    """

    def insert(self, entry: object) -> None:
        """Enqueue one pending re-emission (§6.1 entry shape; Phase 9 mode 7)."""
        ...


class StageContext:
    """Read-only stage inputs plus the two mutable side-effect ports (§2.1).

    One instance per tick. ``mode_config`` is rebound per stage by the pipeline so
    each stage reads only its own slice of the policy. ``virtual_clock`` /
    ``registry_view`` are reserved for the temporal / drift stages (modes 5-7);
    modes 1-4 do not consult them.
    """

    __slots__ = (
        "chaos_subseed",
        "late_buffer",
        "mode_config",
        "recorder",
        "registry_view",
        "shard_id",
        "stream_id",
        "virtual_clock",
        "workspace_id",
    )

    def __init__(
        self,
        *,
        stream_id: str,
        shard_id: int,
        workspace_id: str,
        chaos_subseed: bytes,
        recorder: Recorder,
        late_buffer: LateBuffer | None = None,
        mode_config: ModeConfig | None = None,
        virtual_clock: object | None = None,
        registry_view: object | None = None,
    ) -> None:
        self.stream_id = stream_id
        self.shard_id = shard_id
        self.workspace_id = workspace_id
        self.chaos_subseed = chaos_subseed
        self.recorder = recorder
        self.late_buffer = late_buffer
        self.mode_config = mode_config
        self.virtual_clock = virtual_clock
        self.registry_view = registry_view


class Stage(Protocol):
    """The single mode-stage interface (§2.1). Stateless across ticks (except the
    temporal stages, modes 5-7). ``mode`` is the frozen identifier used for PRF
    keying, ``_df.chaos`` keys, and metrics labels — a read-only property so a
    concrete stage may narrow it to its ``ChaosMode`` literal.
    """

    @property
    def mode(self) -> str:
        """The frozen ChaosMode identifier this stage realises."""
        ...

    def process(
        self, batch: list[InternalEnvelope], ctx: StageContext
    ) -> list[InternalEnvelope]:
        """Transform an ordered envelope batch into this stage's delivery order."""
        ...


class InMemoryRecorder:
    """A pure in-memory :class:`Recorder` for deterministic tests (no Postgres).

    Idempotent on ``injection_id`` (CR-7): a re-recorded id is silently ignored,
    mirroring the deterministic-id dedup the Postgres recorder gets for free.
    """

    __slots__ = ("_by_id", "records")

    def __init__(self) -> None:
        self.records: list[InjectionRecord] = []
        self._by_id: set[str] = set()

    def record(self, injection: InjectionRecord) -> None:
        injection_id = injection["injection_id"]
        if injection_id in self._by_id:
            return
        self._by_id.add(injection_id)
        self.records.append(injection)
