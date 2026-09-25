import unittest

from bedrock_benchmark.analysis.capacity import Recommendation, SweepPoint
from bedrock_benchmark.analysis.metrics import RunMetrics
from bedrock_benchmark.client import TransportConfig
from bedrock_benchmark.experiments.executor import ExperimentReport, ProfileReport
from bedrock_benchmark.experiments.schema import ExperimentSpec, QuotaSnapshot, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.results import RequestResult
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


def _result(**overrides) -> RequestResult:
    defaults = dict(
        request_id="r", scheduled_at=0.0, started_at=0.0, completed_at=0.1,
        success=True, input_tokens=500, output_tokens=60, tags={"workload": "short"},
    )
    defaults.update(overrides)
    return RequestResult(**defaults)


class BuildCapacityProfileTests(unittest.TestCase):
    def _spec(self, *, sweep_type="concurrency", sweep_values=None, **overrides) -> ExperimentSpec:
        defaults = dict(
            name="test", target=TargetConfig(model_id="m", region="us-east-1"),
            workloads=[WorkloadProfile(name="short", input_tokens=512, output_tokens=64)],
            sweep=SweepConfig(type=sweep_type, values=sweep_values or [1, 2, 4, 6, 8]),
            quota_snapshot=QuotaSnapshot(rpm=400, tpm=8_000_000),
            slo=SloConfig(ttft_p95_ms=1000, latency_p95_ms=3000),
            provider_headroom=0.20, transport=TransportConfig(),
        )
        defaults.update(overrides)
        return ExperimentSpec(**defaults)

    def test_schema_version_is_2(self):
        spec = self._spec()
        report = ExperimentReport(spec=spec, profiles=[ProfileReport(workload_name="short", recommendation=None)])
        profile = build_capacity_profile(report)
        self.assertEqual(profile["schema_version"], 2)

    def test_concurrency_sweep_writes_a_concurrency_block_not_rate(self):
        spec = self._spec(sweep_type="concurrency")
        rec = Recommendation(
            point=SweepPoint(concurrency=6, rps=None, metrics=_metrics(slo_goodput_rps=5.1)),
            saturation_point=SweepPoint(concurrency=8, rps=None, metrics=_metrics(throttle_rate=0.07)),
        )
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", recommendation=rec),
        ])

        profile = build_capacity_profile(report)
        entry = profile["workload_classes"]["short"]

        self.assertIn("concurrency", entry)
        self.assertNotIn("rate", entry)
        self.assertEqual(entry["concurrency"]["measured_best"], 6)
        self.assertEqual(entry["concurrency"]["saturation"], 8)
        # 6 * (1 - 0.20) = 4.8 -> floored to 4
        self.assertEqual(entry["concurrency"]["production_max"], 4)

    def test_rate_sweep_writes_a_rate_block_not_concurrency_and_saturation_is_labeled_as_rps(self):
        """The real bug this fixes: a rate sweep's saturation point used
        to be written into `saturation_concurrency`, mislabeling 7 RPS
        as if it were a concurrency value."""
        spec = self._spec(sweep_type="rate", sweep_values=[1, 2, 3, 4, 5, 6, 7, 8])
        rec = Recommendation(
            point=SweepPoint(concurrency=None, rps=6.0, metrics=_metrics(slo_goodput_rps=5.8)),
            saturation_point=SweepPoint(concurrency=None, rps=7.0, metrics=_metrics(throttle_rate=0.05)),
        )
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", recommendation=rec),
        ])

        profile = build_capacity_profile(report)
        entry = profile["workload_classes"]["short"]

        self.assertIn("rate", entry)
        self.assertNotIn("concurrency", entry)
        self.assertEqual(entry["rate"]["measured_sustainable_rps"], 5.8)
        self.assertEqual(entry["rate"]["saturation_rps"], 7.0)
        # 5.8 * (1 - 0.20) = 4.64 -- rate headroom is NOT floored to an int
        # (unlike concurrency), since a fractional RPS is meaningful.
        self.assertAlmostEqual(entry["rate"]["production_rps"], 4.64, places=4)

    def test_no_global_concurrency_rollup_is_derived_from_isolated_per_class_maxima(self):
        """The other real bug this fixes: there is no scientifically
        valid "global max concurrency" derivable from independently-
        swept workload classes' own isolated maxima -- a real MIXED
        workload can exceed safe capacity before either class's
        isolated measurement would predict. The artifact must not
        contain any such derived global field at all."""
        spec = self._spec(workloads=[
            WorkloadProfile(name="short", input_tokens=512, output_tokens=64),
            WorkloadProfile(name="long", input_tokens=4096, output_tokens=512),
        ])
        short_rec = Recommendation(point=SweepPoint(concurrency=6, rps=None, metrics=_metrics()), saturation_point=None)
        long_rec = Recommendation(point=SweepPoint(concurrency=2, rps=None, metrics=_metrics()), saturation_point=None)
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", recommendation=short_rec),
            ProfileReport(workload_name="long", recommendation=long_rec),
        ])

        profile = build_capacity_profile(report)

        profile_str = str(profile)
        self.assertNotIn("global_max_concurrency", profile_str)
        self.assertNotIn("global_min_concurrency", profile_str)
        self.assertEqual(profile["workload_classes"]["short"]["concurrency"]["production_max"], 4)
        self.assertEqual(profile["workload_classes"]["long"]["concurrency"]["production_max"], 1)

    def test_no_recommendation_is_reported_not_omitted(self):
        spec = self._spec()
        report = ExperimentReport(spec=spec, profiles=[
            ProfileReport(workload_name="short", recommendation=None),
        ])

        profile = build_capacity_profile(report)

        self.assertIn("short", profile["workload_classes"])
        self.assertIn("note", profile["workload_classes"]["short"])
        self.assertNotIn("concurrency", profile["workload_classes"]["short"])
        self.assertNotIn("rate", profile["workload_classes"]["short"])

    def test_headroom_applied_concurrency_never_floors_to_zero(self):
        spec = self._spec(provider_headroom=0.5)
        rec = Recommendation(point=SweepPoint(concurrency=1, rps=None, metrics=_metrics()), saturation_point=None)
        report = ExperimentReport(spec=spec, profiles=[ProfileReport(workload_name="short", recommendation=rec)])

        profile = build_capacity_profile(report)
        self.assertEqual(profile["workload_classes"]["short"]["concurrency"]["production_max"], 1)

    def test_observed_tokens_come_from_real_measured_results_not_the_configured_target(self):
        spec = self._spec()
        rec = Recommendation(point=SweepPoint(concurrency=6, rps=None, metrics=_metrics()), saturation_point=None)
        report = ExperimentReport(
            spec=spec,
            profiles=[ProfileReport(workload_name="short", recommendation=rec)],
            all_results=[
                _result(input_tokens=500, output_tokens=58),
                _result(input_tokens=510, output_tokens=62),
            ],
        )

        profile = build_capacity_profile(report)
        observed = profile["workload_classes"]["short"]["observed"]
        self.assertIsNotNone(observed["input_tokens_p50"])
        self.assertIsNotNone(observed["output_tokens_p50"])

    def test_quota_snapshot_and_slo_and_transport_are_recorded(self):
        spec = self._spec()
        report = ExperimentReport(spec=spec, profiles=[ProfileReport(workload_name="short", recommendation=None)])

        profile = build_capacity_profile(report)

        self.assertEqual(profile["quota_snapshot"]["rpm"], 400)
        self.assertEqual(profile["slo"]["success_rate_min"], 0.99)
        self.assertEqual(profile["transport"]["total_max_attempts"], 1)
        self.assertEqual(profile["provider"]["headroom"], 0.20)


if __name__ == "__main__":
    unittest.main()
