import unittest

from bedrock_benchmark.analysis.capacity import LATENCY_EXCEEDANCE_MAX, evaluate
from bedrock_benchmark.analysis.confirmation import limits_for
import random

from bedrock_benchmark.analysis.metrics import compute_run_metrics, quantile_upper_bound
from bedrock_benchmark.results import RequestResult


def _results(n_fast: int, n_slow: int, *, fast=500.0, slow=900.0, ttft_missing: int = 0):
    rs = []
    for i in range(n_fast + n_slow + ttft_missing):
        ttft = fast if i < n_fast else (slow if i < n_fast + n_slow else None)
        rs.append(RequestResult(request_id=str(i), scheduled_at=0.0, started_at=0.0, completed_at=1.0,
                                success=True, ttft_ms=ttft, latency_ms=1000.0, output_tokens=64))
    return rs


def _ttft(results, limit=800.0):
    m = compute_run_metrics(results, duration_s=100.0)
    v = evaluate(m, ttft_p95_slo_ms=limit, success_rate_min=0.0, throttle_rate_max=1.0)
    return next(c for c in v.checks if c.name == "ttft_p95")


class LatencyExceedanceTests(unittest.TestCase):
    """TTFT p95 <= 800ms  <=>  P(TTFT > 800ms) <= 5%, proven with an exact
    one-sided bound -- not just "the sample p95 is under 800"."""

    def test_reviewers_example_500_requests_11_slow_passes(self):
        c = _ttft(_results(489, 11))
        self.assertEqual((c.verdict, c.exceedances, c.n), ("PASS", 11, 500))
        self.assertAlmostEqual(c.exceedance_rate_upper, 0.0362, places=4)   # 2.2% observed, 3.6% bound < 5%

    def test_30_clean_requests_are_inconclusive_not_pass(self):
        c = _ttft(_results(30, 0))
        self.assertEqual((c.verdict, c.reason, c.exceedances), ("INCONCLUSIVE", "insufficient_samples", 0))
        self.assertAlmostEqual(c.exceedance_rate_upper, 0.095, places=3)
        self.assertEqual(c.required_n, 59)

    def test_59_clean_requests_resolve_it(self):
        self.assertEqual(_ttft(_results(59, 0)).verdict, "PASS")
        self.assertEqual(_ttft(_results(58, 0)).verdict, "INCONCLUSIVE")

    def test_required_n_grows_with_observed_exceedances(self):
        c = _ttft(_results(70, 1))
        self.assertEqual((c.verdict, c.required_n), ("INCONCLUSIVE", 93))

    def test_fail_needs_the_exceedance_lower_bound_over_5_percent(self):
        c = _ttft(_results(80, 20))  # 20% over: lower bound well above 5%
        self.assertEqual((c.verdict, c.reason), ("FAIL", "violation_demonstrated"))
        self.assertGreater(c.bad_rate_lower, 0.05)
        c = _ttft(_results(90, 10))  # 10% over in 100: the sample p95 is over, but not demonstrably
        self.assertEqual(c.verdict, "INCONCLUSIVE")
        self.assertLess(c.bad_rate_lower, 0.05)

    def test_sample_p95_under_the_limit_is_not_enough_on_its_own(self):
        """The old rule: 40 requests, 1 slow -> sample p95 under 800 -> PASS.
        Now: 2.5% observed, but the bound is far above 5%."""
        results = _results(39, 1)
        m = compute_run_metrics(results, duration_s=100.0)
        self.assertLess(m.ttft_p95_ms, 800)
        self.assertEqual(_ttft(results).verdict, "INCONCLUSIVE")

    def test_a_missing_measurement_counts_as_an_exceedance(self):
        c = _ttft(_results(100, 0, ttft_missing=30))  # 30/130 unmeasured -> demonstrably > 5%
        self.assertEqual((c.verdict, c.exceedances), ("FAIL", 30))

    def test_nothing_measured_fails_closed(self):
        c = _ttft(_results(0, 0, ttft_missing=20))
        self.assertEqual((c.verdict, c.reason), ("FAIL", "not_measured"))

    def test_only_successful_requests_are_counted(self):
        results = _results(59, 0) + [RequestResult(request_id="t", scheduled_at=0.0, started_at=0.0, completed_at=1.0,
                                                   success=False, throttled=True, latency_ms=50.0)]
        self.assertEqual(_ttft(results).n, 59)

    def test_tpot_and_e2e_use_the_same_rule(self):
        results = _results(59, 0)
        m = compute_run_metrics(results, duration_s=100.0)
        v = evaluate(m, tpot_p95_slo_ms=40.0, latency_p95_slo_ms=3000.0, success_rate_min=0.0, throttle_rate_max=1.0)
        by = {c.name: c for c in v.checks}
        self.assertEqual((by["tpot_p95"].verdict, by["latency_p95"].verdict), ("PASS", "PASS"))
        self.assertEqual(by["latency_p95"].exceedances, 0)


class QuantileUpperBoundTests(unittest.TestCase):
    """The reviewer's framing -- "p95 estimate 742ms, one-sided 95% UCB
    796ms <= 800ms -> PASS" -- as the order-statistic bound, which must
    never disagree with the exceedance-proportion verdict."""

    def test_ucb_is_reported_and_bounds_the_estimate(self):
        rng = random.Random(1)
        results = [RequestResult(request_id=str(i), scheduled_at=0.0, started_at=0.0, completed_at=1.0, success=True,
                                 ttft_ms=rng.gauss(600, 70), latency_ms=1000.0, output_tokens=64) for i in range(400)]
        c = _ttft(results)
        self.assertIsNotNone(c.p95_upper_bound)
        self.assertGreaterEqual(c.p95_upper_bound, c.observed)  # the bound sits above the estimate

    def test_too_few_samples_have_no_ucb(self):
        self.assertIsNone(quantile_upper_bound([100.0] * 58))       # 58 < 59: no sample can bound q95 at 95%
        self.assertEqual(quantile_upper_bound([100.0] * 59), 100.0)

    def test_pass_iff_ucb_within_the_limit(self):
        """Exact duality, checked on random samples of many sizes."""
        rng = random.Random(7)
        for trial in range(300):
            n = rng.choice([40, 59, 80, 150, 400])
            values = [rng.lognormvariate(6.4, 0.25) for _ in range(n)]
            limit = rng.uniform(500, 1200)
            results = [RequestResult(request_id=str(i), scheduled_at=0.0, started_at=0.0, completed_at=1.0,
                                     success=True, ttft_ms=v, latency_ms=2000.0, output_tokens=64)
                       for i, v in enumerate(values)]
            c = _ttft(results, limit=limit)
            ucb_ok = c.p95_upper_bound is not None and c.p95_upper_bound <= limit
            with self.subTest(trial=trial, n=n):
                self.assertEqual(c.verdict == "PASS", ucb_ok)


class ConfirmationPlanCoversLatencyTests(unittest.TestCase):
    def test_latency_checks_are_planned_like_rate_checks(self):
        gate = dict(throttle_rate_max=0.01, success_rate_min=0.99, ttft_p95_slo_ms=800, tpot_p95_slo_ms=40)
        limits = {l.name: l.max_bad_rate for l in limits_for(gate, None, None)}
        self.assertEqual(limits["ttft_p95"], LATENCY_EXCEEDANCE_MAX)
        self.assertEqual(limits["tpot_p95"], 0.05)
        self.assertNotIn("latency_p95", limits)


if __name__ == "__main__":
    unittest.main()
