import unittest

from bedrock_benchmark.analysis.capacity import Recommendation, SweepPoint
from bedrock_benchmark.analysis.metrics import RunMetrics
from bedrock_benchmark.experiments.executor import ExperimentReport, ProfileReport
from bedrock_benchmark.experiments.schema import ExperimentSpec, QuotaConfig, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.workload import WorkloadProfile


def _metrics(**overrides) -> RunMetrics:
    defaults = dict(
        n=100, success_rate=1.0, throttle_rate=0.0, timeout_rate=0.0,
        request_throughput_rps=5.0, token_throughput_tps=100.0,
        latency_p50_ms=100.0, latency_p95_ms=200.0, latency_p99_ms=300.0,
        slo_goodput_rps=5.1, slo_efficiency=1.0,
    )
    defaults.update(overrides)
    return RunMetrics(**defaults)


class BuildCapacityProfileTests(unittest.TestCase):
    def _spec(self, **overrides) -> ExperimentSpec:
        defaults = dict(
            name="test", target=TargetConfig(model_id="m", region="us-east-1"),
            workloads=[WorkloadProfile(name="short", input_tokens=512, output_tokens=64)],
            sweep=SweepConfig(type="concurrency", values=[1, 2, 4, 6, 8]),
            quota=QuotaConfig(rpm=400, tpm=8_000_000), slo=SloConfig(ttft_p95_ms=1000, latency_p95_ms=3000),
            provider_headroom=0.20,
        )
        defaults.update(overrides)
        return ExperimentSpec(**defaults)

    def test_recommended_profile_includes_measured_and_headroom_values(self):
        spec = self._spec()
        rec = Recommendation(
            point=SweepPoint(concurrency=6, rps=None, metrics=_metrics(slo_goodput_rps=5.1)),
            saturation_point=SweepPoint(concurrency=8, rps=None, metrics=_metrics(throttle_rate=0.07)),
        )
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", points=[], recommendation=rec),
        ])

        profile = build_capacity_profile(report)

        self.assertEqual(profile["schema_version"], 1)
        self.assertEqual(profile["model"]["model_id"], "m")
        self.assertEqual(profile["quota"]["rpm"], 400)
        self.assertEqual(profile["profiles"]["short"]["recommended_concurrency"], 6)
        self.assertEqual(profile["profiles"]["short"]["saturation_concurrency"], 8)
        self.assertEqual(profile["profiles"]["short"]["sustainable_rps"], 5.1)
        # 6 * (1 - 0.20) = 4.8 -> floored to 4
        self.assertEqual(profile["recommendation"]["gateway"]["classes"]["short"]["max_concurrency"], 4)
        self.assertEqual(profile["recommendation"]["gateway"]["global_max_concurrency"], 6)

    def test_no_recommendation_is_reported_not_omitted(self):
        spec = self._spec()
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", points=[], recommendation=None),
        ])

        profile = build_capacity_profile(report)

        self.assertIn("short", profile["profiles"])
        self.assertIsNone(profile["profiles"]["short"]["recommended_concurrency"])
        self.assertIn("note", profile["profiles"]["short"])
        self.assertNotIn("short", profile["recommendation"]["gateway"]["classes"])

    def test_headroom_applied_concurrency_never_floors_to_zero(self):
        """A recommended concurrency of 1 with 20% headroom would floor
        to 0 without the max(1, ...) guard -- a gateway class with
        max_concurrency=0 could never admit a single request."""
        spec = self._spec(provider_headroom=0.5)
        rec = Recommendation(
            point=SweepPoint(concurrency=1, rps=None, metrics=_metrics()),
            saturation_point=None,
        )
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", points=[], recommendation=rec),
        ])

        profile = build_capacity_profile(report)
        self.assertEqual(profile["recommendation"]["gateway"]["classes"]["short"]["max_concurrency"], 1)


if __name__ == "__main__":
    unittest.main()
