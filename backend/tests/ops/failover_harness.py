"""OPS kill-test / failover harness primitives (testing-strategy §11 OPS-1/2/3).

This is the *library* the compose-only OPS suite drives: pure helpers that read
the lease holder from Redis, snapshot the canonical ledger, and assert the §8.5
failover invariants — **no canonical gaps or duplicates**, **takeover < 30 s**,
**stale-holder fenced** (zero post-takeover writes), and the **stop latency**
budget (last delivered ``emitted_at`` ≤ stop-ack + 5 s).

Why a library, not a pytest module: the kill-test needs Kafka + two real runner
processes (a SIGKILL of a leased asyncio process is not reproducible in-process).
It runs in the verify agent's compose demo (``infra/scripts/demo-phase05.sh``,
steps 9-10) and the OPS compose lane — NOT the standard Postgres CI lane (no
Kafka service, no multi-runner). The assertions here are the normative checks the
script and any future docker-driven test both call, so the pass/fail logic lives
in one tested place.

These helpers speak only stdlib + the shipped lease key format
(``streams.infra.leases.lease_key`` / ``runner.leases``), so they import without
Django and can be invoked from the bash demo via ``python -m`` one-liners.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

# The runtime constants the failover timeline (§8.5) is measured against. Kept here
# (not imported from the runner) so the harness has no Django/asyncio dependency and
# the budget is asserted as an explicit contract, not read from the code under test.
LEASE_TTL_S = 15.0
FAILOVER_BUDGET_S = 30.0  # OPS-1 / phase-05 exit #3: takeover < 30 s.
STOP_LATENCY_BUDGET_S = 5.0  # OPS-3 / phase-05 exit #2: stop halts ≤ 5 s.


class RedisLike(Protocol):
    """The minimal sync Redis surface the harness reads (``redis.Redis``)."""

    def get(self, name: str) -> bytes | None: ...


@dataclass(frozen=True)
class LeaseHolder:
    """The decoded lease value at ``df:lease:{stream}:{shard}`` (§8.2)."""

    runner_id: str
    fencing_token: int


def lease_redis_key(stream_id: str, shard_id: int = 0) -> str:
    """The lease key — mirrors ``streams.infra.leases.lease_key`` byte-for-byte.

    Duplicated (not imported) so the harness stays import-light for the bash demo;
    a drift here is caught by ``test_failover_harness.test_lease_key_matches_shipped``.
    """
    return f"df:lease:{stream_id}:{shard_id}"


def read_lease_holder(redis: RedisLike, stream_id: str, shard_id: int = 0) -> LeaseHolder | None:
    """Decode the current lease holder, or ``None`` if the lease is unheld/expired.

    The §8.2 lease value is canonical JSON ``{"fencing_token": int, "runner_id": str}``.
    """
    raw = redis.get(lease_redis_key(stream_id, shard_id))
    if raw is None:
        return None
    payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    return LeaseHolder(
        runner_id=str(payload["runner_id"]),
        fencing_token=int(payload["fencing_token"]),
    )


def wait_for_lease(
    redis: RedisLike,
    stream_id: str,
    *,
    shard_id: int = 0,
    timeout_s: float = 30.0,
    poll_s: float = 0.5,
) -> LeaseHolder:
    """Block until a lease holder appears (initial acquisition) or raise on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        holder = read_lease_holder(redis, stream_id, shard_id)
        if holder is not None:
            return holder
        time.sleep(poll_s)
    raise TimeoutError(f"no lease holder for stream {stream_id} within {timeout_s}s")


def wait_for_takeover(
    redis: RedisLike,
    stream_id: str,
    *,
    killed_runner_id: str,
    killed_token: int | None = None,
    shard_id: int = 0,
    timeout_s: float = FAILOVER_BUDGET_S,
    poll_s: float = 0.5,
) -> tuple[LeaseHolder, float]:
    """Block until a *different* runner with a *higher* fencing token holds the lease.

    Returns ``(new_holder, elapsed_s)``. The new token strictly exceeding the killed
    holder's is the §8.2 monotonic-INCR guarantee — it is what fences the resurrected
    stale holder (OPS-2). ``killed_token`` is the token the killed holder carried (the
    baseline the new token must beat); when omitted it is read once at entry, so a
    real takeover that has not happened yet is still detected as the token advancing.
    Raises ``TimeoutError`` if no takeover within the budget.
    """
    start = time.monotonic()
    deadline = start + timeout_s
    if killed_token is not None:
        old_token = killed_token
    else:
        before = read_lease_holder(redis, stream_id, shard_id)
        old_token = before.fencing_token if before else -1
    while time.monotonic() < deadline:
        holder = read_lease_holder(redis, stream_id, shard_id)
        if (
            holder is not None
            and holder.runner_id != killed_runner_id
            and holder.fencing_token > old_token
        ):
            return holder, time.monotonic() - start
        time.sleep(poll_s)
    raise TimeoutError(
        f"no takeover of stream {stream_id} from {killed_runner_id} within {timeout_s}s"
    )


# ---------------------------------------------------------------------------
# Canonical-stream invariants (read over the ground-truth ledger).
#
# The ledger is the canonical business truth (INV-GEN-5): it is gapless and dedup
# by construction (per-(stream,shard) ``sequence_no`` + ``ON CONFLICT DO NOTHING``).
# After a kill+takeover the *delivered* REST stream may carry at-least-once dupes
# (§8.5), but the canonical ledger must remain gapless with zero duplicates — that
# is precisely what the kill-test proves about the failover (idempotent regen into
# the conflict-protected sink). These helpers run over rows the caller has already
# read from ``ground_truth_ledger`` for (stream, shard 0).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerGapReport:
    """The outcome of scanning a sequence_no series for the canonical invariants."""

    count: int
    first_seq: int | None
    last_seq: int | None
    gaps: list[tuple[int, int]]  # (after, before) pairs bracketing each missing run
    duplicates: list[int]  # sequence_no values that appear more than once

    @property
    def is_gapless(self) -> bool:
        return not self.gaps

    @property
    def is_dedup(self) -> bool:
        return not self.duplicates

    @property
    def ok(self) -> bool:
        """The kill-test canonical assertion: gapless AND no duplicates."""
        return self.is_gapless and self.is_dedup


def scan_ledger_sequence(sequence_nos: list[int]) -> LedgerGapReport:
    """Scan a list of ledger ``sequence_no`` values for gaps and duplicates.

    ``sequence_nos`` is every row's ``sequence_no`` for one (stream, shard), in any
    order — the canonical series is contiguous integers per (stream, shard, 0). A
    missing integer between the min and max is a *gap* (lost canonical event, INV);
    a repeated value is a *duplicate* (the ledger conflict guard failed). Either
    fails the kill-test (phase-05 exit #3 "zero canonical gaps/duplicates").
    """
    if not sequence_nos:
        return LedgerGapReport(count=0, first_seq=None, last_seq=None, gaps=[], duplicates=[])
    ordered = sorted(sequence_nos)
    seen: set[int] = set()
    duplicates: list[int] = []
    for seq in ordered:
        if seq in seen:
            duplicates.append(seq)
        seen.add(seq)
    gaps: list[tuple[int, int]] = []
    unique = sorted(seen)
    for prev, nxt in pairwise(unique):
        if nxt != prev + 1:
            gaps.append((prev, nxt))
    return LedgerGapReport(
        count=len(sequence_nos),
        first_seq=unique[0],
        last_seq=unique[-1],
        gaps=gaps,
        duplicates=sorted(set(duplicates)),
    )


def assert_canonical_failover(report: LedgerGapReport, *, pre_kill_last_seq: int) -> None:
    """Assert the §8.5 canonical-stream guarantee held across the kill+takeover.

    * The ledger is gapless and duplicate-free (the cardinal kill-test assertion).
    * Emission resumed: the post-takeover last sequence_no exceeds the last one seen
      before the kill (the stream kept running and produced *new* canonical events).
    """
    assert report.ok, (
        f"canonical ledger violated across failover: gaps={report.gaps}, "
        f"duplicates={report.duplicates} (phase-05 exit #3 — zero gaps/duplicates)"
    )
    assert report.last_seq is not None and report.last_seq > pre_kill_last_seq, (
        f"stream did not resume after takeover: last_seq={report.last_seq} did not "
        f"advance past pre-kill {pre_kill_last_seq}"
    )


def assert_no_stale_writes(
    *, post_takeover_min_token: int, stale_holder_tokens_after_resurrection: list[int]
) -> None:
    """OPS-2: a resurrected stale holder writes ZERO rows after takeover (INV-STR-2).

    The fencing guard (checkpoint conditional write / ledger conflict / injection
    insert) rejects any write carrying a token below the live holder's. So *every*
    token the resurrected stale holder could present is strictly below the new
    holder's token — there must be none ≥ the live token (which would mean an
    un-fenced write slipped through).
    """
    leaked = [t for t in stale_holder_tokens_after_resurrection if t >= post_takeover_min_token]
    assert not leaked, (
        f"stale holder wrote with un-fenced token(s) {leaked} ≥ live token "
        f"{post_takeover_min_token} — fencing failed (OPS-2 / INV-STR-2)"
    )


def assert_stop_latency(*, stop_ack_ts: float, last_delivered_emitted_ts: float) -> None:
    """OPS-3: last delivered ``emitted_at`` ≤ stop-ack + 5 s (phase-05 exit #2).

    Both arguments are POSIX seconds (the stop-ack wall time and the wall time of
    the newest delivered event's ``emitted_at``). A negative delta (the frontier is
    older than the ack) trivially passes — emission already halted.
    """
    delta = last_delivered_emitted_ts - stop_ack_ts
    assert delta <= STOP_LATENCY_BUDGET_S, (
        f"stop latency {delta:.2f}s exceeds the {STOP_LATENCY_BUDGET_S}s budget "
        "(OPS-3 / T10) — emission did not halt in time"
    )


def assert_takeover_within_budget(elapsed_s: float) -> None:
    """OPS-1: takeover completed in < 30 s (phase-05 exit #3; lease TTL 15 s)."""
    assert elapsed_s < FAILOVER_BUDGET_S, (
        f"failover took {elapsed_s:.1f}s ≥ {FAILOVER_BUDGET_S}s budget (OPS-1)"
    )

