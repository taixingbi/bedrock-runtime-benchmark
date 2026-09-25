import unittest

from bedrock_benchmark.analysis.metrics import compute_run_metrics, percentile
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


if __name__ == "__main__":
    unittest.main()
