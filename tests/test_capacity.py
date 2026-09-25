import unittest

from bedrock_benchmark.analysis.capacity import SweepPoint, apply_headroom, meets_slo, point_meets_slo, recommend
from bedrock_benchmark.analysis.metrics import RunMetrics


def _metrics(**overrides) -> RunMetrics:
    defaults = dict(
        n=100, success_rate=1.0, throttle_rate=0.0, timeout_rate=0.0,
        request_throughput_rps=5.0, token_throughput_tps=100.0,
        latency_p50_ms=100.0, latency_p95_ms=200.0, latency_p99_ms=300.0,
        slo_goodput_rps=5.0, slo_efficiency=1.0,
    )
    defaults.update(overrides)
    return RunMetrics(**defaults)


class MeetsSloTests(unittest.TestCase):
    def test_empty_run_never_meets_slo(self):
        self.assertFalse(meets_slo(_metrics(n=0)))

    def test_low_success_rate_fails(self):
        self.assertFalse(meets_slo(_metrics(success_rate=0.90), success_rate_min=0.99))

    def test_high_throttle_rate_fails(self):
        self.assertFalse(meets_slo(_metrics(throttle_rate=0.05), throttle_rate_max=0.001))

    def test_ttft_over_slo_fails(self):
        self.assertFalse(meets_slo(_metrics(ttft_p95_ms=1500.0), ttft_p95_slo_ms=1000.0))

    def test_missing_ttft_with_a_configured_ttft_slo_fails_not_passes(self):
        """The real bug: a TTFT SLO is configured but ttft_p95_ms is
        None (e.g. stream: false, or every result failed before its
        first token) -- this is a missing/invalid measurement against
        a configured SLO, not automatic compliance. The old code's
        `metrics.ttft_p95_ms is not None and ...` skipped the check
        entirely when None, silently passing."""
        self.assertFalse(meets_slo(_metrics(ttft_p95_ms=None), ttft_p95_slo_ms=1000.0))

    def test_no_ttft_slo_configured_means_missing_ttft_is_fine(self):
        """Without a configured TTFT SLO, a None ttft_p95_ms (e.g. a
        non-streaming run that never intended to measure TTFT) must
        NOT fail -- only a CONFIGURED-but-unmeasured SLO is a
        violation."""
        self.assertTrue(meets_slo(_metrics(ttft_p95_ms=None)))

    def test_latency_over_slo_fails(self):
        self.assertFalse(meets_slo(_metrics(latency_p95_ms=5000.0), latency_p95_slo_ms=3000.0))

    def test_all_conditions_satisfied_passes(self):
        self.assertTrue(meets_slo(
            _metrics(success_rate=1.0, throttle_rate=0.0, ttft_p95_ms=500.0, latency_p95_ms=1000.0),
            ttft_p95_slo_ms=1000.0, latency_p95_slo_ms=3000.0,
        ))


class GateOnBoundsTests(unittest.TestCase):
    def test_clean_point_with_too_few_samples_fails_when_gating_on_bounds(self):
        m = _metrics(n=540, throttle_rate=0.0, throttle_rate_upper=0.005, success_rate_lower=0.993)
        self.assertTrue(meets_slo(m, throttle_rate_max=0.001))
        self.assertFalse(meets_slo(m, throttle_rate_max=0.001, gate_on_bounds=True))

    def test_resolved_point_passes_when_gating_on_bounds(self):
        m = _metrics(n=3000, throttle_rate=0.0, throttle_rate_upper=0.0009, success_rate_lower=0.999)
        self.assertTrue(meets_slo(m, throttle_rate_max=0.001, gate_on_bounds=True))

    def test_missing_bounds_fail_closed(self):
        self.assertFalse(meets_slo(_metrics(), gate_on_bounds=True))


class MixedPointTests(unittest.TestCase):
    def test_a_failing_class_fails_the_point_even_if_the_blend_passes(self):
        """70/30 short/long: the blended p95 can look fine while the
        long class alone blows the latency SLO."""
        point = SweepPoint(
            concurrency=None, rps=5.0, metrics=_metrics(latency_p95_ms=2500.0),
            class_metrics={"short": _metrics(latency_p95_ms=800.0), "long": _metrics(latency_p95_ms=4200.0)},
        )
        self.assertTrue(meets_slo(point.metrics, latency_p95_slo_ms=3000.0))
        self.assertFalse(point_meets_slo(point, latency_p95_slo_ms=3000.0))
        self.assertIsNone(recommend([point], latency_p95_slo_ms=3000.0))


class RecommendTests(unittest.TestCase):
    def test_picks_highest_slo_goodput_among_passing_points(self):
        points = [
            SweepPoint(concurrency=1, rps=None, metrics=_metrics(slo_goodput_rps=1.8)),
            SweepPoint(concurrency=2, rps=None, metrics=_metrics(slo_goodput_rps=3.1)),
            SweepPoint(concurrency=4, rps=None, metrics=_metrics(slo_goodput_rps=4.7)),
            SweepPoint(concurrency=6, rps=None, metrics=_metrics(slo_goodput_rps=5.1)),
        ]
        rec = recommend(points)
        self.assertEqual(rec.point.concurrency, 6)

    def test_a_point_that_fails_slo_is_excluded_even_with_higher_raw_throughput(self):
        """The exact case this repo's README calls out: C=8 has higher
        raw throughput but blows the SLO via throttling -- C=6 must win."""
        points = [
            SweepPoint(concurrency=6, rps=None, metrics=_metrics(slo_goodput_rps=5.1, throttle_rate=0.0)),
            SweepPoint(concurrency=8, rps=None, metrics=_metrics(slo_goodput_rps=3.9, throttle_rate=0.07)),
        ]
        rec = recommend(points, throttle_rate_max=0.001)
        self.assertEqual(rec.point.concurrency, 6)
        self.assertEqual(rec.saturation_point.concurrency, 8)

    def test_no_passing_point_returns_none(self):
        points = [SweepPoint(concurrency=1, rps=None, metrics=_metrics(success_rate=0.5))]
        self.assertIsNone(recommend(points, success_rate_min=0.99))

    def test_saturation_point_is_none_when_every_point_passes(self):
        points = [SweepPoint(concurrency=1, rps=None, metrics=_metrics())]
        rec = recommend(points)
        self.assertIsNone(rec.saturation_point)

    def test_rate_sweep_uses_rps_as_the_sort_key(self):
        points = [
            SweepPoint(concurrency=None, rps=1.0, metrics=_metrics(slo_goodput_rps=1.0)),
            SweepPoint(concurrency=None, rps=5.0, metrics=_metrics(slo_goodput_rps=5.0)),
        ]
        rec = recommend(points)
        self.assertEqual(rec.point.rps, 5.0)

    def test_ties_broken_toward_lower_concurrency(self):
        points = [
            SweepPoint(concurrency=2, rps=None, metrics=_metrics(slo_goodput_rps=5.0)),
            SweepPoint(concurrency=6, rps=None, metrics=_metrics(slo_goodput_rps=5.0)),
        ]
        rec = recommend(points)
        self.assertEqual(rec.point.concurrency, 2)


class ApplyHeadroomTests(unittest.TestCase):
    def test_reduces_by_the_headroom_fraction(self):
        self.assertEqual(apply_headroom(10.0, headroom=0.20), 8.0)

    def test_zero_headroom_is_a_no_op(self):
        self.assertEqual(apply_headroom(10.0, headroom=0.0), 10.0)


if __name__ == "__main__":
    unittest.main()
