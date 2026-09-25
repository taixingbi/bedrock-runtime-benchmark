import unittest

from bedrock_benchmark.analysis.metrics import (
    MeasurementWindow, compute_run_metrics, min_samples_to_resolve_rate, percentile, wilson_lower, wilson_upper,
)
from bedrock_benchmark.results import RequestResult


def _result(**overrides) -> RequestResult:
    defaults = dict(
        request_id="r", scheduled_at=0.0, started_at=0.0, completed_at=0.1,
        success=True, latency_ms=100.0, output_tokens=10,
    )
    defaults.update(overrides)
    return RequestResult(**defaults)


class PercentileTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(percentile([], 95), 0.0)

    def test_p50_odd_length(self):
        self.assertEqual(percentile([1, 2, 3], 50), 2)


class ComputeRunMetricsTests(unittest.TestCase):
    def test_empty_results(self):
        m = compute_run_metrics([], duration_s=10.0)
        self.assertEqual(m.n, 0)
        self.assertEqual(m.success_rate, 0.0)

    def test_success_throttle_timeout_rates(self):
        results = [
            _result(success=True),
            _result(success=False, throttled=True),
            _result(success=False, timed_out=True),
        ]
        m = compute_run_metrics(results, duration_s=10.0)
        self.assertAlmostEqual(m.success_rate, 1 / 3, places=4)
        self.assertAlmostEqual(m.throttle_rate, 1 / 3, places=4)
        self.assertAlmostEqual(m.timeout_rate, 1 / 3, places=4)

    def test_request_and_token_throughput(self):
        results = [_result(success=True, output_tokens=100) for _ in range(5)]
        m = compute_run_metrics(results, duration_s=10.0)
        self.assertEqual(m.request_throughput_rps, 0.5)
        self.assertEqual(m.token_throughput_tps, 50.0)

    def test_no_slo_configured_means_no_goodput(self):
        m = compute_run_metrics([_result()], duration_s=10.0)
        self.assertIsNone(m.slo_goodput_rps)
        self.assertIsNone(m.slo_efficiency)

    def test_slo_goodput_excludes_failures_and_slow_requests(self):
        results = [
            _result(success=True, latency_ms=100.0),   # within SLO
            _result(success=True, latency_ms=5000.0),  # too slow
            _result(success=False, latency_ms=50.0),   # failed
        ]
        m = compute_run_metrics(results, duration_s=10.0, latency_slo_ms=1000.0)
        self.assertEqual(m.slo_goodput_rps, 0.1)  # 1 good / 10s

    def test_slo_efficiency_uses_offered_rps_when_given(self):
        results = [_result(success=True, latency_ms=100.0)]
        m = compute_run_metrics(results, duration_s=10.0, latency_slo_ms=1000.0, offered_rps=5.0)
        # slo_goodput_rps = 1/10 = 0.1, efficiency = 0.1/5.0 = 0.02
        self.assertEqual(m.slo_efficiency, 0.02)

    def test_ttft_slo_also_gates_goodput(self):
        results = [
            _result(success=True, latency_ms=100.0, ttft_ms=2000.0),  # TTFT too slow
        ]
        m = compute_run_metrics(results, duration_s=10.0, ttft_slo_ms=1000.0)
        self.assertEqual(m.slo_goodput_rps, 0.0)

    def test_missing_ttft_with_a_configured_ttft_slo_does_not_count_as_goodput(self):
        """The real bug: a request with no ttft_ms at all (non-
        streaming, or a streaming call that never captured one) must
        NOT count toward slo_goodput_rps just because a TTFT SLO
        happens to be configured -- a missing measurement is not
        compliance."""
        results = [_result(success=True, latency_ms=100.0, ttft_ms=None)]
        m = compute_run_metrics(results, duration_s=10.0, ttft_slo_ms=1000.0)
        self.assertEqual(m.slo_goodput_rps, 0.0)



class MeasurementWindowTests(unittest.TestCase):
    """warmup -> window [100, 110) -> drain."""
    W = MeasurementWindow(start=100.0, end=110.0)

    def test_drain_completions_do_not_inflate_throughput(self):
        """The real bug: every success / duration_s counted requests
        that completed AFTER the window closed (the closed-loop drain)
        in the numerator without extending the denominator."""
        in_window = [_result(scheduled_at=100 + i, started_at=100 + i, completed_at=100.5 + i) for i in range(5)]
        drained = [_result(scheduled_at=109.8, started_at=109.8, completed_at=111.0) for _ in range(5)]
        m = compute_run_metrics(in_window + drained, windows=[self.W])
        self.assertEqual(m.request_throughput_rps, 0.5)  # 5 completed in-window / 10s, not 10/10s

    def test_drained_requests_still_count_toward_latency_and_rates(self):
        """Excluding in-flight-at-close requests from the rate/latency
        population would drop exactly the slow tail an SLO catches."""
        fast = _result(scheduled_at=101, completed_at=101.1, latency_ms=100.0)
        slow = _result(scheduled_at=109.9, completed_at=115.0, latency_ms=5100.0, success=False, throttled=True)
        m = compute_run_metrics([fast, slow], windows=[self.W])
        self.assertEqual(m.n, 2)
        self.assertEqual(m.throttle_rate, 0.5)
        self.assertGreater(m.latency_p99_ms, 5000)

    def test_warmup_requests_are_excluded(self):
        warmup = _result(scheduled_at=95, completed_at=95.5, success=False, throttled=True)
        measured = _result(scheduled_at=101, completed_at=101.5)
        m = compute_run_metrics([warmup, measured], windows=[self.W])
        self.assertEqual(m.n, 1)
        self.assertEqual(m.throttle_rate, 0.0)

    def test_multiple_windows_pool_counts_and_duration(self):
        w2 = MeasurementWindow(start=200.0, end=210.0)
        results = [_result(scheduled_at=101, completed_at=101.5), _result(scheduled_at=201, completed_at=201.5)]
        m = compute_run_metrics(results, windows=[self.W, w2])
        self.assertEqual(m.n, 2)
        self.assertEqual(m.measured_duration_s, 20.0)
        self.assertEqual(m.request_throughput_rps, 0.1)


class ConfidenceBoundTests(unittest.TestCase):
    def test_zero_throttles_in_540_does_not_resolve_a_0_1_percent_slo(self):
        """The reviewer's example: 6 rps x 90s ~= 540 requests."""
        self.assertGreater(wilson_upper(0, 540), 0.001)

    def test_enough_zero_throttle_samples_do_resolve_it(self):
        n = min_samples_to_resolve_rate(0.001)
        self.assertLessEqual(wilson_upper(0, n), 0.001)
        self.assertGreater(wilson_upper(0, n - 1), 0.001)
        self.assertTrue(2500 < n < 3000)

    def test_bounds_bracket_the_point_estimate(self):
        self.assertLess(wilson_lower(990, 1000), 0.99)
        self.assertGreater(wilson_upper(10, 1000), 0.01)

    def test_metrics_carry_counts_and_bounds(self):
        results = [_result() for _ in range(99)] + [_result(success=False, throttled=True)]
        m = compute_run_metrics(results, duration_s=10.0)
        self.assertEqual(m.n_throttled, 1)
        self.assertGreater(m.throttle_rate_upper, m.throttle_rate)
        self.assertLess(m.success_rate_lower, m.success_rate)
        self.assertEqual(m.bound_confidence, 0.95)


if __name__ == "__main__":
    unittest.main()
