"""Unit tests for the OPS kill-test/failover harness (testing-strategy §11).

The kill-test itself is compose-only (Kafka + two runners; the verify agent's
``demo-phase05.sh`` steps 9-10). But the *pass/fail logic* the script leans on —
canonical gap/dup detection, takeover timing, fencing, stop latency — is pure and
MUST be gated in the standard CI lane so a regression in the assertion logic
cannot let a real failover violation pass silently. These tests pin that logic and
prove the harness's lease-key format matches the shipped runtime byte-for-byte (a
drift would make the harness read the wrong Redis key and never see the holder).
"""

from __future__ import annotations

import json

import pytest

from tests.ops import failover_harness as fh


class _FakeRedis:
    """A minimal in-memory ``get`` surface (the harness only reads)."""

    def __init__(self, store: dict[str, bytes] | None = None) -> None:
        self._store = store or {}

    def get(self, name: str) -> bytes | None:
        return self._store.get(name)


def _lease_value(runner_id: str, token: int) -> bytes:
    return json.dumps({"fencing_token": token, "runner_id": runner_id}).encode()


def test_lease_key_matches_shipped() -> None:
    """The harness lease key must equal ``streams.infra.leases.lease_key`` exactly.

    A drift here is silent and fatal: the harness would poll a key nothing writes,
    see no holder, and the kill-test would hang or falsely pass.
    """
    from streams.infra import leases as stream_leases

    sid = "7b1e9c3a-2f54-4d08-a6b9-1c2d3e4f5a6b"
    assert fh.lease_redis_key(sid, 0) == stream_leases.lease_key(sid, 0)


def test_read_and_wait_for_lease() -> None:
    sid = "s1"
    redis = _FakeRedis({fh.lease_redis_key(sid, 0): _lease_value("runner-A", 7)})
    holder = fh.read_lease_holder(redis, sid)
    assert holder is not None
    assert holder.runner_id == "runner-A"
    assert holder.fencing_token == 7
    assert fh.wait_for_lease(redis, sid, timeout_s=1.0).runner_id == "runner-A"


def test_read_lease_none_when_unheld() -> None:
    assert fh.read_lease_holder(_FakeRedis(), "absent") is None


def test_wait_for_lease_times_out() -> None:
    with pytest.raises(TimeoutError):
        fh.wait_for_lease(_FakeRedis(), "absent", timeout_s=0.3, poll_s=0.05)


def test_wait_for_takeover_detects_higher_token_new_runner() -> None:
    sid = "s2"
    # The new holder is a different runner with a strictly higher fencing token.
    redis = _FakeRedis({fh.lease_redis_key(sid, 0): _lease_value("runner-B", 8)})
    holder, elapsed = fh.wait_for_takeover(
        redis, sid, killed_runner_id="runner-A", killed_token=7, timeout_s=1.0, poll_s=0.05
    )
    assert holder.runner_id == "runner-B"
    assert holder.fencing_token == 8
    assert elapsed >= 0.0
    fh.assert_takeover_within_budget(elapsed)


def test_wait_for_takeover_ignores_same_runner_or_lower_token() -> None:
    sid = "s3"
    # Still the killed runner (resurrected, somehow re-set) → not a takeover.
    redis = _FakeRedis({fh.lease_redis_key(sid, 0): _lease_value("runner-A", 5)})
    with pytest.raises(TimeoutError):
        fh.wait_for_takeover(
            redis, sid, killed_runner_id="runner-A", killed_token=5, timeout_s=0.3, poll_s=0.05
        )


def test_scan_ledger_gapless_clean() -> None:
    report = fh.scan_ledger_sequence([3, 1, 2, 4, 5])
    assert report.ok
    assert report.is_gapless
    assert report.is_dedup
    assert (report.first_seq, report.last_seq, report.count) == (1, 5, 5)


def test_scan_ledger_detects_gap() -> None:
    report = fh.scan_ledger_sequence([1, 2, 4, 5])  # 3 missing
    assert not report.is_gapless
    assert report.gaps == [(2, 4)]
    assert not report.ok


def test_scan_ledger_detects_duplicate() -> None:
    report = fh.scan_ledger_sequence([1, 2, 2, 3])
    assert not report.is_dedup
    assert report.duplicates == [2]
    assert not report.ok


def test_assert_canonical_failover_passes_on_gapless_advance() -> None:
    report = fh.scan_ledger_sequence(list(range(1, 21)))  # 1..20, clean
    fh.assert_canonical_failover(report, pre_kill_last_seq=10)  # resumed past 10


def test_assert_canonical_failover_fails_on_gap() -> None:
    report = fh.scan_ledger_sequence([1, 2, 3, 5, 6])  # gap at 4
    with pytest.raises(AssertionError, match="gaps"):
        fh.assert_canonical_failover(report, pre_kill_last_seq=2)


def test_assert_canonical_failover_fails_when_not_resumed() -> None:
    report = fh.scan_ledger_sequence(list(range(1, 11)))  # clean, last=10
    with pytest.raises(AssertionError, match="did not resume"):
        fh.assert_canonical_failover(report, pre_kill_last_seq=10)  # no advance


def test_assert_no_stale_writes_passes_when_fenced() -> None:
    # The stale holder only ever held tokens below the live token → all fenced.
    fh.assert_no_stale_writes(
        post_takeover_min_token=8, stale_holder_tokens_after_resurrection=[7, 7, 6]
    )


def test_assert_no_stale_writes_fails_on_unfenced_write() -> None:
    with pytest.raises(AssertionError, match="fencing failed"):
        fh.assert_no_stale_writes(
            post_takeover_min_token=8, stale_holder_tokens_after_resurrection=[7, 9]
        )


def test_assert_stop_latency_within_budget() -> None:
    fh.assert_stop_latency(stop_ack_ts=1000.0, last_delivered_emitted_ts=1004.0)
    # A frontier older than the ack trivially passes.
    fh.assert_stop_latency(stop_ack_ts=1000.0, last_delivered_emitted_ts=999.0)


def test_assert_stop_latency_exceeded() -> None:
    with pytest.raises(AssertionError, match="stop latency"):
        fh.assert_stop_latency(stop_ack_ts=1000.0, last_delivered_emitted_ts=1006.5)


def test_assert_takeover_within_budget_fails_when_slow() -> None:
    with pytest.raises(AssertionError, match="failover took"):
        fh.assert_takeover_within_budget(31.0)
