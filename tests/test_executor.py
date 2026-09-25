import unittest

from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.experiments.executor import run_experiment
from bedrock_benchmark.experiments.schema import ExperimentSpec, MixConfig, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.workload import WorkloadProfile

from .fakes import FakeBedrockRuntimeClient


def _spec(**overrides) -> ExperimentSpec:
    defaults = dict(
        name="t", target=TargetConfig(model_id="m"),
        workloads=[WorkloadProfile(name="short", input_tokens=100, output_tokens=16)],
        sweep=SweepConfig(type="rate", values=[40.0]),
        slo=SloConfig(latency_p95_ms=3000), duration_s=0.2, warmup_s=0.05, repetitions=2, stream=False, seed=1,
    )
    defaults.update(overrides)
    return ExperimentSpec(**defaults)


class RunExperimentTests(unittest.IsolatedAsyncioTestCase):
    async def test_repetitions_pool_windows_and_tag_results(self):
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())

        report = await run_experiment(_spec(), target=target)

        point = report.profiles[0].points[0]
        self.assertEqual(len(point.repetitions), 2)
        self.assertAlmostEqual(point.metrics.measured_duration_s, 0.4, places=3)
        self.assertEqual({r.tags["repetition"] for r in report.all_results}, {0, 1})
        measured = [r for r in report.all_results if r.tags["measured"]]
        self.assertEqual(point.metrics.n, len(measured))
        self.assertTrue(all("window_start" in r.tags for r in report.all_results))

    async def test_confidence_gating_rejects_an_unresolvable_clean_point(self):
        """~16 clean requests can never demonstrate a 0.1% throttle SLO."""
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())

        report = await run_experiment(
            _spec(slo=SloConfig(latency_p95_ms=3000, confidence=0.95)), target=target,
        )

        self.assertEqual(report.profiles[0].points[0].metrics.n_throttled, 0)
        self.assertIsNone(report.profiles[0].recommendation)

    async def test_mix_sweeps_once_with_per_class_metrics(self):
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(
            workloads=[
                WorkloadProfile(name="short", input_tokens=100, output_tokens=16),
                WorkloadProfile(name="long", input_tokens=400, output_tokens=64),
            ],
            mix=MixConfig(name="blend", weights={"short": 3, "long": 1}),
            sweep=SweepConfig(type="rate", values=[80.0]),
        )

        report = await run_experiment(spec, target=target)

        self.assertEqual([p.workload_name for p in report.profiles], ["blend"])
        point = report.profiles[0].points[0]
        self.assertEqual(set(point.class_metrics), {"short", "long"})
        self.assertEqual(point.class_metrics["short"].n + point.class_metrics["long"].n, point.metrics.n)
        self.assertEqual({r.tags["subject"] for r in report.all_results}, {"blend"})

        profile = build_capacity_profile(report)
        self.assertIn("blend", profile["mixed_workloads"])
        self.assertEqual(profile["mixed_workloads"]["blend"]["shares"], {"short": 0.75, "long": 0.25})
        # Classes measured only inside a mix get validation, never an isolated envelope.
        self.assertNotIn("rate", profile["workload_classes"]["short"])
        self.assertIn("workload_validation", profile["workload_classes"]["short"])


if __name__ == "__main__":
    unittest.main()
