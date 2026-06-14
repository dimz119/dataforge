"""Unit tests for the Phase-6 stream-control harness (testing-strategy §11, §13).

The OPS-5 / OPS-8 / XCH / SOAK-200 round-trips are compose-only (Kafka + the ws
process + a Redis channel layer; the verify agent's demo-phase06.sh + the nightly
soak lane). Their pass/fail LOGIC, however, is pure and gated here so a regression in
the assertion math cannot let a real stream-control violation pass on the PR lane.
Each test pins one rung: the dynamic-TPS stopwatch, drop-notice reconciliation,
cross-channel content equality (clean + chaos), and the soak regression thresholds.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.ops import stream_control_harness as h

# -- OPS-5 dynamic TPS effectiveness ------------------------------------------


def test_observed_rate_counts_trailing_window() -> None:
    # 500 events all within the last second → ~500 tps.
    base = 1_700_000_000_000
    ms = [base + i * 2 for i in range(500)]  # 2 ms apart → spans ~1 s
    rate = h.observed_rate_from_emitted_at(ms, window_s=1.0)
    assert rate == pytest.approx(500, abs=2)


def test_time_to_reach_target_finds_first_in_tolerance() -> None:
    samples = [
        h.TpsObservation(0.0, 10.0),
        h.TpsObservation(1.0, 120.0),
        h.TpsObservation(1.5, 410.0),  # within 20 % of 500 → reached
        h.TpsObservation(2.5, 500.0),
    ]
    assert h.time_to_reach_target(samples, target_tps=500.0) == 1.5


def test_assert_tps_effective_passes_within_budget() -> None:
    samples = [
        h.TpsObservation(0.0, 10.0),
        h.TpsObservation(1.0, 480.0),  # reached by 1 s
        h.TpsObservation(2.0, 505.0),
    ]
    assert h.assert_tps_effective_within_budget(samples, target_tps=500.0) == 1.0


def test_assert_tps_effective_fails_when_too_slow() -> None:
    samples = [
        h.TpsObservation(0.0, 10.0),
        h.TpsObservation(2.0, 90.0),
        h.TpsObservation(3.0, 480.0),  # only reached at 3 s > 2 s budget
    ]
    with pytest.raises(AssertionError, match=r"took 3\.00 s"):
        h.assert_tps_effective_within_budget(samples, target_tps=500.0)


def test_assert_tps_effective_fails_when_never_reached() -> None:
    samples = [h.TpsObservation(float(t), 60.0) for t in range(4)]
    with pytest.raises(AssertionError, match="never reached"):
        h.assert_tps_effective_within_budget(samples, target_tps=500.0)


# -- OPS-8 WS backpressure reconciliation -------------------------------------


def test_drop_counts_reconcile_exactly() -> None:
    h.assert_drop_counts_reconcile(produced=1000, delivered=970, drop_notice_total=30)


def test_drop_counts_silent_loss_fails() -> None:
    # 30 frames vanished without a drop-notice → silent loss.
    with pytest.raises(AssertionError, match="silent loss"):
        h.assert_drop_counts_reconcile(produced=1000, delivered=970, drop_notice_total=0)


def test_drop_counts_overcount_fails() -> None:
    with pytest.raises(AssertionError, match="over-counted"):
        h.assert_drop_counts_reconcile(produced=1000, delivered=1000, drop_notice_total=5)


def test_memory_bounded_passes_at_cap() -> None:
    h.assert_memory_bounded(queue_len=1000, capacity=1000)


def test_memory_bounded_fails_over_cap() -> None:
    with pytest.raises(AssertionError, match="not bounding memory"):
        h.assert_memory_bounded(queue_len=1001, capacity=1000)


# -- XCH cross-channel content equality ---------------------------------------


def _ev(eid: str, pk: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event_id": eid, "partition_key": pk, "event_type": "order_placed", "payload": payload}


def test_xch_clean_identical_sets_passes() -> None:
    rest = [_ev("a", "p1", {"x": 1}), _ev("b", "p1", {"x": 2}), _ev("c", "p2", {"x": 3})]
    # WS delivers the same content, wire key order differs (dict order is irrelevant).
    ws = [
        {"payload": {"x": 1}, "event_type": "order_placed", "partition_key": "p1", "event_id": "a"},
        _ev("b", "p1", {"x": 2}),
        _ev("c", "p2", {"x": 3}),
    ]
    report = h.compare_channels(rest, ws)
    assert report.rest_ids == report.ws_ids
    h.assert_xch(report, ws_drop_notice_total=0, clean=True)


def test_xch_detects_content_mismatch() -> None:
    rest = [_ev("a", "p1", {"x": 1})]
    ws = [_ev("a", "p1", {"x": 999})]  # same id, different content
    report = h.compare_channels(rest, ws)
    with pytest.raises(AssertionError, match="differ in content"):
        h.assert_xch(report, ws_drop_notice_total=0, clean=True)


def test_xch_clean_fails_on_drop() -> None:
    rest = [_ev("a", "p1", {"x": 1}), _ev("b", "p1", {"x": 2})]
    ws = [_ev("a", "p1", {"x": 1})]  # WS missing b at a clean rate
    report = h.compare_channels(rest, ws)
    with pytest.raises(AssertionError, match="should have zero drops"):
        h.assert_xch(report, ws_drop_notice_total=0, clean=True)


def test_xch_chaos_subset_reconciles_with_drop_notice() -> None:
    rest = [_ev(c, "p1", {"x": i}) for i, c in enumerate("abcde")]
    ws = [rest[0], rest[1], rest[4]]  # WS dropped c, d (2 events)
    report = h.compare_channels(rest, ws)
    # The drop notice reported exactly the 2 missing → reconciles (INV-DEL-5).
    h.assert_xch(report, ws_drop_notice_total=2, clean=False)


def test_xch_chaos_fails_when_drop_notice_miscounts() -> None:
    rest = [_ev(c, "p1", {"x": i}) for i, c in enumerate("abcde")]
    ws = [rest[0], rest[4]]  # WS dropped 3
    report = h.compare_channels(rest, ws)
    with pytest.raises(AssertionError, match="reconcile exactly"):
        h.assert_xch(report, ws_drop_notice_total=2, clean=False)  # under-reported


def test_xch_fails_when_ws_has_event_rest_lacks() -> None:
    rest = [_ev("a", "p1", {"x": 1})]
    ws = [_ev("a", "p1", {"x": 1}), _ev("ghost", "p1", {"x": 2})]
    report = h.compare_channels(rest, ws)
    with pytest.raises(AssertionError, match="not in REST"):
        h.assert_xch(report, ws_drop_notice_total=0, clean=False)


def test_xch_detects_partition_reorder() -> None:
    rest = [_ev("a", "p1", {"n": 1}), _ev("b", "p1", {"n": 2}), _ev("c", "p1", {"n": 3})]
    # WS delivers b before a within the same partition_key — a FIFO violation (S-3).
    ws = [_ev("b", "p1", {"n": 2}), _ev("a", "p1", {"n": 1}), _ev("c", "p1", {"n": 3})]
    report = h.compare_channels(rest, ws)
    assert report.partition_order_ok is False
    with pytest.raises(AssertionError, match="reordered"):
        h.assert_xch(report, ws_drop_notice_total=0, clean=True)


# -- SOAK-200 regression analysis ---------------------------------------------


def test_linear_regression_slope_basic() -> None:
    assert h.linear_regression_slope([0, 1, 2, 3], [0, 2, 4, 6]) == pytest.approx(2.0)
    assert h.linear_regression_slope([0, 1, 2], [5, 5, 5]) == pytest.approx(0.0)


def test_percentile_nearest_rank() -> None:
    assert h.percentile([1, 2, 3, 4, 5], 0.99) == 5
    assert h.percentile([1, 2, 3, 4, 5], 0.5) == 3


def test_rss_stable_passes_on_flat_series() -> None:
    samples = [h.RssSample(float(m), 100.0 + 0.001 * m) for m in range(60)]
    slope, growth = h.assert_rss_stable(samples)
    assert slope < h.SOAK_RSS_SLOPE_MAX_MIB_PER_MIN
    assert growth < h.SOAK_RSS_GROWTH_MAX_FRAC


def test_rss_stable_fails_on_leak_slope() -> None:
    samples = [h.RssSample(float(m), 100.0 + 2.0 * m) for m in range(30)]  # 2 MiB/min
    with pytest.raises(AssertionError, match="MiB/min"):
        h.assert_rss_stable(samples)


def test_rss_stable_fails_on_large_growth_even_if_slope_small() -> None:
    # 100 → 130 over 600 minutes: slope 0.05 MiB/min (< 1) but 30 % growth (≥ 10 %).
    samples = [h.RssSample(float(m), 100.0 + 0.05 * m) for m in range(601)]
    with pytest.raises(AssertionError, match="grew"):
        h.assert_rss_stable(samples)


def test_lag_healthy_passes_on_flat_low_lag() -> None:
    samples = [h.LagSample(float(t), 1.0 + 0.0001 * (t % 3)) for t in range(120)]
    _slope, p99 = h.assert_lag_healthy(samples)
    assert p99 < h.SOAK_LAG_P99_MAX_S


def test_lag_healthy_fails_on_rising_lag() -> None:
    samples = [h.LagSample(float(t), 0.5 + 0.05 * t) for t in range(120)]  # rising
    with pytest.raises(AssertionError, match="trending up"):
        h.assert_lag_healthy(samples)


def test_lag_healthy_fails_on_high_p99() -> None:
    # A flat (non-trending) series whose tail percentile breaches 5 s: a symmetric
    # pair of spikes near the middle keeps the regression slope ~0 while p99 is high.
    samples = [h.LagSample(float(t), 1.0) for t in range(101)]
    samples[49] = h.LagSample(49.0, 9.0)
    samples[51] = h.LagSample(51.0, 9.0)
    slope = h.linear_regression_slope([s.t_s for s in samples], [s.lag_s for s in samples])
    assert abs(slope) <= 0.001  # genuinely flat — so the p99 branch is what fires
    with pytest.raises(AssertionError, match="p99"):
        h.assert_lag_healthy(samples)


def test_tallies_reconcile_passes_when_equal() -> None:
    h.assert_tallies_reconcile(rest_total=720_000, ws_total=720_000, stats_total=720_000)


def test_tallies_reconcile_fails_on_disagreement() -> None:
    with pytest.raises(AssertionError, match="tallies disagree"):
        h.assert_tallies_reconcile(rest_total=720_000, ws_total=719_999, stats_total=720_000)


def test_staleness_ok_passes_under_5s() -> None:
    h.assert_staleness_ok(4.9)


def test_staleness_fails_over_5s() -> None:
    with pytest.raises(AssertionError, match="staleness"):
        h.assert_staleness_ok(6.0)


def test_zero_error_logs_passes_empty() -> None:
    h.assert_zero_error_logs([])


def test_zero_error_logs_fails_on_any() -> None:
    with pytest.raises(AssertionError, match="ERROR log"):
        h.assert_zero_error_logs(["2026-06-14 ERROR runner: boom"])
