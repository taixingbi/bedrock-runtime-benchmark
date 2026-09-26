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

    async def test_unresolvable_clean_point_is_inconclusive_not_failed(self):
        """~16 clean requests can't DEMONSTRATE a 0.1% throttle SLO -- but
        insufficient evidence isn't failure: the point is INCONCLUSIVE,
        still eligible, and says how many requests would settle it."""
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())

        report = await run_experiment(
            _spec(slo=SloConfig(latency_p95_ms=3000, confidence=0.95)), target=target,
        )

        profile = report.profiles[0]
        self.assertEqual(profile.points[0].metrics.n_throttled, 0)
        rec = profile.recommendation
        self.assertIsNotNone(rec)
        self.assertEqual(rec.verdict.verdict, "INCONCLUSIVE")
        self.assertIsNone(rec.confirmed_point)
        throttle = next(c for c in rec.verdict.inconclusive_checks if c.name == "throttle_rate")
        self.assertEqual(throttle.reason, "insufficient_samples")
        self.assertGreater(throttle.required_n, 2000)
        self.assertEqual(profile.verdicts[0].verdict, "INCONCLUSIVE")

    # Loose rate limits so a fake 0.2s window can reach the first look:
    # throttle <= 5%, success >= 90% -> first look at a few dozen requests.
    LOOSE = SloConfig(latency_p95_ms=3000, throttle_rate_max=0.05, success_rate_min=0.9)

    async def test_confirmation_uses_only_its_own_independent_data(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="rate", values=[200.0, 400.0]), repetitions=1, slo=self.LOOSE,
                     confirmation=ConfirmationConfig(max_looks=2, max_repetitions=20, max_requests=10**6))

        report = await run_experiment(spec, target=target)

        profile = report.profiles[0]
        rec = profile.recommendation
        self.assertEqual(rec.confirmation_source, "confirmation")
        [result] = profile.confirmations
        self.assertEqual((result.value, result.verdict, result.stop_reason), (400.0, "PASS", "confirmed"))
        self.assertGreaterEqual(result.n, profile.confirmation_plan.look_schedule[0])
        # The confirmed point's metrics are confirmation data ONLY -- the
        # discovery requests at 400 rps are not pooled in.
        conf_measured = [r for r in report.all_results
                         if r.tags["phase"] == "confirmation" and r.tags["measured"]]
        self.assertEqual(rec.confirmed_point.metrics.n, len(conf_measured))
        self.assertEqual(rec.confirmed_point.metrics.bound_confidence, profile.confirmation_plan.per_look_confidence)
        # Discovery points are untouched by confirmation.
        self.assertTrue(all(len(p.repetitions) == 1 and p.phase == "discovery" for p in profile.points))

    async def test_caps_without_a_pass_are_inconclusive_never_pass(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="rate", values=[200.0]), repetitions=1,
                     confirmation=ConfirmationConfig(max_repetitions=3))  # gold-strict default 0.1% throttle

        report = await run_experiment(spec, target=target)

        [result] = report.profiles[0].confirmations
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertEqual(result.stop_reason, "unreachable_within_caps")  # ~40 req/rep can't reach ~3,700
        self.assertEqual(result.repetitions, 0)                          # no calls spent on a hopeless candidate
        self.assertIsNone(report.profiles[0].recommendation.confirmed_point)
        rate = build_capacity_profile(report)["workload_classes"]["short"]["rate"]
        self.assertIsNone(rate["production_sustained_rps"])
        self.assertEqual(rate["confirmation_source"], "confirmation")

    async def test_violation_during_confirmation_fails_the_candidate(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig

        class ThrottlesAfterDiscovery(BedrockConverseTarget):
            throttle = False

            async def invoke(self, request):
                result = await super().invoke(request)
                if self.throttle:
                    result.success, result.throttled, result.error_code = False, True, "ThrottlingException"
                return result

        target = ThrottlesAfterDiscovery(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="rate", values=[200.0]), repetitions=1, slo=self.LOOSE,
                     confirmation=ConfirmationConfig(max_repetitions=20, max_requests=10**6))

        def after_discovery(subject, value, point):
            target.throttle = True

        report = await run_experiment(spec, target=target, on_progress=after_discovery)

        [result] = report.profiles[0].confirmations
        self.assertEqual((result.verdict, result.stop_reason, result.repetitions), ("FAIL", "observed_violation", 1))
        self.assertIsNone(report.profiles[0].recommendation.confirmed_point)
        # Discovery itself saw no violation (not FAIL -- ~40 requests are too
        # few to PASS even the loose limit): only confirmation data failed it.
        self.assertNotEqual(report.profiles[0].verdicts[0].verdict, "FAIL")

    async def test_several_candidates_are_tested_lowest_first_and_stop_at_the_first_non_pass(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="rate", values=[100.0, 200.0, 400.0]), repetitions=1, slo=self.LOOSE,
                     confirmation=ConfirmationConfig(candidates=2, max_repetitions=20, max_requests=10**6))

        report = await run_experiment(spec, target=target)

        results = report.profiles[0].confirmations
        self.assertEqual([r.value for r in results], [200.0, 400.0])  # the two highest, ascending
        self.assertTrue(all(r.verdict == "PASS" for r in results))
        self.assertEqual(report.profiles[0].recommendation.confirmed_point.rps, 400.0)

    async def test_without_confirmation_discovery_is_a_fixed_sequence_test(self):
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        report = await run_experiment(_spec(slo=self.LOOSE), target=target)
        self.assertEqual(report.profiles[0].recommendation.confirmation_source, "discovery_fixed_sequence")
        self.assertEqual(report.profiles[0].confirmations, [])

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

    async def test_mix_judges_each_class_against_its_own_slo_profile(self):
        """long class: 5s latency -- fails an interactive 3s SLO but
        passes its 10s long_generation profile."""
        from bedrock_benchmark.experiments.schema import SloConfig as Slo

        class LongIsSlow(BedrockConverseTarget):
            async def invoke(self, request):
                result = await super().invoke(request)
                if request.max_tokens == 64:
                    result.latency_ms = 5000.0
                return result

        def spec(long_profile):
            return _spec(
                workloads=[WorkloadProfile(name="short", input_tokens=100, output_tokens=16),
                           WorkloadProfile(name="long", input_tokens=400, output_tokens=64, slo_profile=long_profile)],
                mix=MixConfig(name="blend", weights={"short": 1, "long": 1}),
                sweep=SweepConfig(type="rate", values=[80.0]),
                slo=Slo(latency_p95_ms=3000), slo_profiles={"long_generation": Slo(latency_p95_ms=10000),
                                                             "strict": Slo(latency_p95_ms=3000)},
            )

        lenient = await run_experiment(spec("long_generation"), target=LongIsSlow(model_id="m", client=FakeBedrockRuntimeClient()))
        strict = await run_experiment(spec("strict"), target=LongIsSlow(model_id="m", client=FakeBedrockRuntimeClient()))
        self.assertIsNotNone(lenient.profiles[0].recommendation)
        self.assertIsNone(strict.profiles[0].recommendation)

    async def test_point_that_outgrew_the_thread_pool_is_flagged_and_excluded(self):
        from bedrock_benchmark.client import TransportConfig

        # The blocking call itself is slow, so calls pile up waiting for the one thread.
        class SlowClient(FakeBedrockRuntimeClient):
            def converse(self, **kwargs):
                import time as t
                t.sleep(0.05)
                return super().converse(**kwargs)

        target = BedrockConverseTarget(model_id="m", client=SlowClient(), transport=TransportConfig(max_connections=1))
        report = await run_experiment(_spec(sweep=SweepConfig(type="rate", values=[80.0]), repetitions=1), target=target)
        point = report.profiles[0].points[0]
        self.assertGreater(point.peak_outstanding, 1)
        self.assertTrue(point.client_limited)
        self.assertIsNone(report.profiles[0].recommendation)
        profile = build_capacity_profile(report)
        self.assertEqual(profile["workload_classes"]["short"]["client_limited_points"], [80.0])
        self.assertEqual(profile["transport"]["executor_workers"], 64)  # the spec's, as recorded
        target.close()


if __name__ == "__main__":
    unittest.main()
