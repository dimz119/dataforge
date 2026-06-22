"""Zero-ERROR soak assertion (phase-11 exit #8, SOAK-200 class — short variant).

Exit criterion #8 requires *"zero ERROR lines over SOAK-200"*. The full SOAK-200 is a
nightly/gate-run 200-iteration soak against the live stack; this is its fast,
deterministic in-process surrogate: drive a bounded multi-shard generation workload
(the data-plane hot path) and the standard logging seams under the real shared logging
chain, capturing every line, and assert **zero ``error``/``critical`` lines** are
emitted on a clean run.

A non-zero ERROR count on a nominal workload is exactly what SOAK-200 catches (a latent
exception path firing under sustained load); proving zero on a representative bounded
run gates that property in the PR lane. The soak deliberately exercises the LV-1 tick
summary + LV-4 dedup helpers and a multi-shard batch so the assertion spans the lines a
real data-plane window would emit.

Pure engine + the shared logging chain (no Postgres, no Redis); runs in either lane.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from config import logging as dflog
from tests.golden.harness_shards import run_shard
from tests.seeds import SEED_SOAK

# A bounded soak: a handful of shards each producing a modest batch, plus repeated
# data-plane logging through the volume-limited helpers — enough to surface a latent
# error path without the runtime of the nightly SOAK-200.
SOAK_SHARDS = 4
SOAK_EVENTS_PER_SHARD = 500
SOAK_TICK_ITERATIONS = 200


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Configure the shared chain to a buffer; reset context + limiters around the test."""
    dflog._configured = False
    structlog.contextvars.clear_contextvars()
    dflog.reset_volume_limiters()
    dflog.configure_logging(service="runner", env_name="dev", release="soak", level="DEBUG")
    buffer = io.StringIO()
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    yield buffer
    structlog.contextvars.clear_contextvars()
    dflog.reset_volume_limiters()


def _error_lines(buffer: io.StringIO) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for line in buffer.getvalue().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if str(obj.get("level", "")).lower() in ("error", "critical"):
            out.append(obj)
    return out


def test_bounded_multishard_soak_emits_zero_error_lines(captured_logs: io.StringIO) -> None:
    """A clean bounded multi-shard generation + logging workload emits no ERROR lines.

    Drives ``SOAK_SHARDS`` shards to ``SOAK_EVENTS_PER_SHARD`` each and runs the
    LV-1/LV-4 data-plane logging helpers ``SOAK_TICK_ITERATIONS`` times, then asserts
    the captured stream contains zero ``error``/``critical`` lines (exit #8 SOAK-200
    "zero ERROR lines")."""
    log = structlog.get_logger("dataforge.runner")

    total_events = 0
    for shard_id in range(SOAK_SHARDS):
        dflog.bind_log_context(stream_id="soak-stream", shard_id=shard_id)
        events = run_shard(
            seed=SEED_SOAK, shard_id=shard_id, shard_count=SOAK_SHARDS,
            max_events=SOAK_EVENTS_PER_SHARD,
        )
        total_events += len(events)
        # The sanctioned per-window data-plane lines (LV-1 + lifecycle).
        dflog.emit_tick_summary(
            log, stream_id="soak-stream", shard_id=shard_id, events=len(events)
        )
        dflog.emit_stream_state_changed(
            log, stream_id="soak-stream", from_state="starting", to_state="running",
            reason="boot",
        )
        dflog.unbind_log_context()

    # Sustained logging through the volume limiters (a window's worth of activity).
    for index in range(SOAK_TICK_ITERATIONS):
        dflog.emit_tick_summary(log, stream_id="soak-stream", shard_id=0, tick=index)
        dflog.emit_deduped_warning(
            log, "buffer.commit.slow", workspace_id="soak-ws", stream_id="soak-stream",
            lag_ms=index,
        )
        log.info("runner.tick.detail", tick=index)

    assert total_events > 0, "the soak generated no events (nothing was exercised)"
    errors = _error_lines(captured_logs)
    assert not errors, (
        f"the bounded soak emitted {len(errors)} ERROR/CRITICAL line(s) on a clean run "
        f"(exit #8 requires zero): first={errors[0] if errors else None}"
    )


def test_soak_actually_produced_log_output(captured_logs: io.StringIO) -> None:
    """Potency guard: the soak emits lines (an empty capture would pass vacuously)."""
    log = structlog.get_logger("dataforge.runner")
    dflog.emit_stream_state_changed(
        log, stream_id="soak-stream", from_state="created", to_state="starting",
        reason="start",
    )
    log.info("runner.boot", pid=1234)
    lines = [line for line in captured_logs.getvalue().splitlines() if line.strip()]
    assert len(lines) >= 2, "the soak harness captured no log output"
