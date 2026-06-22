"""OPS-1/2 multi-shard kill-test assertion logic (phase-11 exit #6).

The live kill-test (SIGKILL one shard's worker mid-load) needs Kafka + two real
runner processes and runs in the compose OPS lane / ``demo-phase05.sh`` — NOT the
standard Postgres CI lane. But the *pass/fail logic* the script leans on must be
gated here so a regression in the assertion can never let a real multi-shard
failover violation pass silently, exactly as :mod:`tests.ops.test_failover_harness`
does for the single-shard case.

What phase-11 exit #6 requires of a multi-shard failover:

* the killed shard's lease expires ≤ 15 s and is taken over < 30 s by a *different*
  runner with a *strictly greater* fencing token (``df_runner_lease_takeovers_total
  {reason=failover}`` increments);
* the killed shard's canonical ledger has zero gaps and zero duplicates across the
  takeover, and emission resumed (last seq advanced past the pre-kill frontier);
* a resurrected stale holder writes ZERO rows (fenced by the higher token);
* **the OTHER shards are untouched** — their lease holders + fencing tokens do not
  change when one shard's worker is killed (shard isolation: a per-shard lease keyed
  ``df:lease:{stream}:{shard}`` means killing shard ``k`` cannot disturb shard ``j``).

These reuse the shipped :mod:`tests.ops.failover_harness` primitives (the same code
the compose script calls), driven over a fake per-shard Redis so the logic is gated
without Kafka.
"""

from __future__ import annotations

import json

import pytest

from tests.ops import failover_harness as fh

N_SHARDS = 4


class _FakeRedis:
    """An in-memory per-key ``get`` surface (the harness only reads leases)."""

    def __init__(self, store: dict[str, bytes] | None = None) -> None:
        self._store = store or {}

    def get(self, name: str) -> bytes | None:
        return self._store.get(name)

    def set(self, name: str, value: bytes) -> None:
        self._store[name] = value


def _lease_value(runner_id: str, token: int) -> bytes:
    return json.dumps({"fencing_token": token, "runner_id": runner_id}).encode()


def _seed_multishard_leases(redis: _FakeRedis, stream_id: str, holder: str, token: int) -> None:
    """Plant one held lease per shard (the steady state before any kill)."""
    for shard_id in range(N_SHARDS):
        redis.set(fh.lease_redis_key(stream_id, shard_id), _lease_value(holder, token))


def test_per_shard_lease_keys_are_distinct_and_match_shipped() -> None:
    """Each shard has its own ``df:lease:{stream}:{shard}`` key (shard isolation).

    The harness key must equal the shipped ``streams.infra.leases.lease_key`` for
    every shard, and the per-shard keys must be distinct — the structural basis for
    "killing shard k cannot disturb shard j" (a shared key would couple them)."""
    from streams.infra import leases as stream_leases

    sid = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"
    keys = {fh.lease_redis_key(sid, shard) for shard in range(N_SHARDS)}
    assert len(keys) == N_SHARDS, "per-shard lease keys collided"
    for shard in range(N_SHARDS):
        assert fh.lease_redis_key(sid, shard) == stream_leases.lease_key(sid, shard)


def test_takeover_of_one_shard_within_budget_with_higher_token() -> None:
    """Killed shard 1 is taken over < 30 s by a new runner with a strictly higher token.

    The OPS-1 budget (< 30 s, lease TTL 15 s) + the OPS-2 fencing guarantee (the new
    token strictly exceeds the killed holder's via the §8.2 monotonic INCR), asserted
    for one shard of an N-way split."""
    sid = "stream-x"
    redis = _FakeRedis()
    _seed_multishard_leases(redis, sid, holder="runner-A", token=7)
    # Shard 1 fails over to runner-B with token 8 (> 7).
    redis.set(fh.lease_redis_key(sid, 1), _lease_value("runner-B", 8))
    holder, elapsed = fh.wait_for_takeover(
        redis, sid, killed_runner_id="runner-A", killed_token=7,
        shard_id=1, timeout_s=1.0, poll_s=0.05,
    )
    assert holder.runner_id == "runner-B"
    assert holder.fencing_token == 8
    fh.assert_takeover_within_budget(elapsed)


def test_killing_one_shard_leaves_other_shards_untouched() -> None:
    """The non-killed shards keep their holder + fencing token (shard isolation).

    Phase-11 exit #6 "other shards untouched": with per-shard leases, a takeover on
    shard 1 must not advance shard 0/2/3's holder or token. Assert each survivor's
    lease is byte-identical before and after the shard-1 failover."""
    sid = "stream-y"
    redis = _FakeRedis()
    _seed_multishard_leases(redis, sid, holder="runner-A", token=5)
    before = {
        shard: fh.read_lease_holder(redis, sid, shard) for shard in range(N_SHARDS)
    }
    # Shard 2 fails over; nothing else is rewritten.
    redis.set(fh.lease_redis_key(sid, 2), _lease_value("runner-C", 6))
    after = {
        shard: fh.read_lease_holder(redis, sid, shard) for shard in range(N_SHARDS)
    }
    for shard in (0, 1, 3):
        assert after[shard] == before[shard], (
            f"shard {shard} lease changed when shard 2 failed over — shards are not "
            "isolated (killing one shard disturbed another)"
        )
    assert after[2] is not None and after[2].runner_id == "runner-C"
    assert after[2].fencing_token == 6 > before[2].fencing_token  # type: ignore[union-attr]


def test_killed_shard_canonical_ledger_gapless_across_takeover() -> None:
    """The killed shard's per-(stream,shard) ledger stays gapless + dedup, and resumed.

    After a kill+takeover the delivered REST stream may carry at-least-once dupes, but
    the canonical ledger for that one shard must remain gapless with zero duplicates
    and have advanced past the pre-kill frontier (idempotent regen into the
    conflict-protected sink) — the cardinal kill-test assertion, per shard."""
    # Shard 3's ledger: contiguous 1..40, pre-kill frontier was 18; emission resumed.
    report = fh.scan_ledger_sequence(list(range(1, 41)))
    fh.assert_canonical_failover(report, pre_kill_last_seq=18)
    assert report.ok and report.last_seq == 40


def test_killed_shard_ledger_gap_is_detected() -> None:
    """A canonical gap on the killed shard fails the assertion (negative control)."""
    report = fh.scan_ledger_sequence([1, 2, 3, 5, 6, 7])  # 4 lost
    with pytest.raises(AssertionError, match="gaps"):
        fh.assert_canonical_failover(report, pre_kill_last_seq=2)


def test_resurrected_stale_shard_holder_is_fenced() -> None:
    """A resurrected stale holder on the killed shard writes ZERO rows (OPS-2 fencing).

    Every token the resurrected stale holder could present is strictly below the new
    holder's, so the checkpoint/ledger/injection fencing guard rejects all of them —
    there must be no leaked write ≥ the live token (INV-STR-2)."""
    fh.assert_no_stale_writes(
        post_takeover_min_token=9, stale_holder_tokens_after_resurrection=[8, 8, 7]
    )
    with pytest.raises(AssertionError, match="fencing failed"):
        fh.assert_no_stale_writes(
            post_takeover_min_token=9, stale_holder_tokens_after_resurrection=[8, 10]
        )


def test_each_shard_independently_satisfies_the_budget() -> None:
    """All N shards' takeovers each clear the < 30 s budget (the per-shard exit #6).

    A simultaneous N-shard failover must complete every shard within budget; assert
    the budget check for each shard's measured elapsed (here a fast in-memory takeover
    so each is well under 30 s)."""
    sid = "stream-z"
    redis = _FakeRedis()
    _seed_multishard_leases(redis, sid, holder="runner-A", token=3)
    for shard_id in range(N_SHARDS):
        redis.set(fh.lease_redis_key(sid, shard_id), _lease_value("runner-B", 4))
    for shard_id in range(N_SHARDS):
        _, elapsed = fh.wait_for_takeover(
            redis, sid, killed_runner_id="runner-A", killed_token=3,
            shard_id=shard_id, timeout_s=1.0, poll_s=0.05,
        )
        fh.assert_takeover_within_budget(elapsed)
