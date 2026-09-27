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
        # No confirmation phase ran, so the reason must not point at one:
        # it names where the fixed-sequence test stopped and what it lacked.
        reason = build_capacity_profile(report)["workload_classes"]["short"]["recommendation"]["reason"]
        self.assertIn("discovery only (no `confirmation:` phase)", reason)
        self.assertIn(f"throttle_rate n={throttle.n} < required_n={throttle.required_n}", reason)
        self.assertNotIn("raise the confirmation caps", reason)

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
        # Fixed-count look: decided on EXACTLY the planned N, however many
        # the repetitions produced.
        self.assertEqual(result.decision_n, profile.confirmation_plan.look_schedule[result.looks_used - 1])
        self.assertEqual(result.decision_metrics.n, result.decision_n)
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
        recommendation = build_capacity_profile(report)["workload_classes"]["short"]["recommendation"]
        self.assertIsNone(recommendation["admission_envelope"])
        self.assertIn("confirmation at 200: INCONCLUSIVE (unreachable_within_caps", recommendation["reason"])
        self.assertIn("raise them", recommendation["reason"])
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

    async def test_concurrency_sweep_confirms_its_candidate_with_fresh_data(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="concurrency", values=[2, 4]), repetitions=1, slo=self.LOOSE,
                     confirmation=ConfirmationConfig(max_repetitions=20, max_requests=10**6))

        report = await run_experiment(spec, target=target)

        profile = report.profiles[0]
        [result] = profile.confirmations
        self.assertEqual((result.value, result.verdict, result.stop_reason), (4, "PASS", "confirmed"))
        self.assertEqual(profile.recommendation.confirmation_source, "confirmation")
        self.assertIs(profile.recommendation.confirmed_point, result.point)
        entry = build_capacity_profile(report)["workload_classes"]["short"]
        self.assertEqual(entry["concurrency"]["statistically_confirmed"], 4)
        self.assertEqual(entry["recommendation"]["admission_envelope"]["max_inflight"], 3)  # floor(4 x 0.8)

    async def test_cooldown_separates_discovery_from_confirmation(self):
        from bedrock_benchmark.experiments.schema import ConfirmationConfig
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        spec = _spec(sweep=SweepConfig(type="concurrency", values=[2]), repetitions=1, slo=self.LOOSE,
                     confirmation=ConfirmationConfig(max_repetitions=20, max_requests=10**6, cooldown_s=0.3))

        report = await run_experiment(spec, target=target)

        results = sorted(report.all_results, key=lambda r: r.tags["window_start"])
        discovery_end = max(r.tags["window_start"] for r in results if r.tags["phase"] == "discovery")
        confirmation_start = min(r.tags["window_start"] for r in results if r.tags["phase"] == "confirmation")
        # window_start = rep start + warmup: the gap spans one window, the cooldown and a warmup.
        self.assertGreaterEqual(confirmation_start - discovery_end, spec.duration_s + 0.3 + spec.warmup_s - 0.02)

    async def test_discovery_stops_after_consecutive_fails(self):
        from .fakes import ThrottlingError
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient(error=ThrottlingError()))
        spec = _spec(sweep=SweepConfig(type="concurrency", values=[1, 2, 3, 4], stop_after_fails=2), repetitions=1)

        report = await run_experiment(spec, target=target)

        self.assertEqual([p.concurrency for p in report.profiles[0].points], [1, 2])  # 3 and 4 never sent
        entry = build_capacity_profile(report)["workload_classes"]["short"]
        self.assertEqual(entry["sweep_stopped_early"], {"after_consecutive_fails": 2, "skipped_values": [3, 4]})

    async def test_refinement_bisects_the_bracket_to_the_real_edge(self):
        """Backend edge is exactly C=5. Coarse 1/2/4/8: 4 non-failing, 8
        FAILs; refinement tests 6 (FAIL) then 5 (non-failing) -> the
        observed edge is 5, not the coarse 4, and saturation is 6."""
        from .fakes import ConcurrencyLimitedClient
        target = BedrockConverseTarget(model_id="m", client=ConcurrencyLimitedClient(limit=5, call_s=0.05))
        from bedrock_benchmark.experiments.schema import RefinementConfig
        spec = _spec(sweep=SweepConfig(type="concurrency", values=[1, 2, 4, 8], refinement=RefinementConfig()),
                     repetitions=1, slo=self.LOOSE)

        report = await run_experiment(spec, target=target)

        profile = report.profiles[0]
        self.assertEqual([(p.concurrency, p.phase) for p in profile.points],
                         [(1, "discovery"), (2, "discovery"), (4, "discovery"), (5, "refinement"),
                          (6, "refinement"), (8, "discovery")])
        rec = profile.recommendation
        self.assertEqual((rec.point.concurrency, rec.saturation_point.concurrency), (5, 6))
        refined = [r for r in report.all_results if r.tags["phase"] == "refinement"]
        self.assertTrue(refined and {r.tags["sweep_value"] for r in refined} == {5, 6})

    async def test_four_refinement_points_resolve_the_widest_gap(self):
        """32 non-FAIL / 48 FAIL, true edge 45: 40, 44, 46, 45 -> adjacent
        45 / 46 within max_points 4."""
        from bedrock_benchmark.experiments.schema import RefinementConfig
        from .fakes import ConcurrencyLimitedClient
        # 50 ms calls: long enough that all workers really overlap, so the
        # fake's edge is sharp regardless of thread scheduling.
        target = BedrockConverseTarget(model_id="m", client=ConcurrencyLimitedClient(limit=45, call_s=0.05))
        spec = _spec(sweep=SweepConfig(type="concurrency", values=[1, 32, 48], refinement=RefinementConfig(max_points=4)),
                     repetitions=1, slo=self.LOOSE)

        report = await run_experiment(spec, target=target)

        refined = [p.concurrency for p in report.profiles[0].points if p.phase == "refinement"]
        self.assertEqual(sorted(refined), [40, 44, 45, 46])
        rec = report.profiles[0].recommendation
        self.assertEqual((rec.point.concurrency, rec.saturation_point.concurrency), (45, 46))

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


class CandidateSelectionTests(unittest.TestCase):
    def test_the_rounded_1x_point_is_a_candidate_and_above_ceiling_points_are_not(self):
        """Quota-relative values are rounded to 4 decimals, so 1.0x of a
        400 RPM quota is 6.6667 rps -- a hair above the unrounded ceiling.
        It must still be eligible; 1.25x must not."""
        from bedrock_benchmark.analysis.capacity import SweepAnalysis, SweepPoint, Recommendation
        from bedrock_benchmark.analysis.metrics import RunMetrics
        from bedrock_benchmark.experiments.executor import _candidates
        from bedrock_benchmark.experiments.schema import load_experiment
        from bedrock_benchmark.models import load_models

        spec = load_experiment("experiments/rate-capacity.yaml", load_models(names=["nova-micro"])[0])
        values = spec.sweep_values("short_chat")
        m = RunMetrics(n=1, success_rate=1, throttle_rate=0, timeout_rate=0, request_throughput_rps=1,
                       token_throughput_tps=None, latency_p50_ms=1, latency_p95_ms=1, latency_p99_ms=1)
        points = [SweepPoint(concurrency=None, rps=v, metrics=m) for v in values]
        rec = Recommendation(point=points[-1], saturation_point=None,
                             analysis=SweepAnalysis(status="not_reached", stable_pass_max=values[-1]))
        spec.confirmation.candidates = 2
        self.assertEqual([p.rps for p in _candidates(points, rec, spec, "short_chat", 2)], [5.0, 6.6667])
