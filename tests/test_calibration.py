import unittest

from bedrock_benchmark.calibration import calibrate_profile, probe_counter, resolve_counter
from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.experiments.executor import calibrate_workloads, run_experiment
from bedrock_benchmark.experiments.schema import ExperimentSpec, SloConfig, SweepConfig, TargetConfig
from bedrock_benchmark.report import build_capacity_profile
from bedrock_benchmark.workload import WorkloadProfile

from .fakes import FakeBedrockRuntimeClient

SHORT = WorkloadProfile(name="short", input_tokens=512, output_tokens=64)


def _target(**fake_kwargs) -> BedrockConverseTarget:
    return BedrockConverseTarget(model_id="us.amazon.m-v1:0", client=FakeBedrockRuntimeClient(**fake_kwargs))


def _spec(token_counting="auto", **overrides) -> ExperimentSpec:
    defaults = dict(
        name="t", target=TargetConfig(model_id="us.amazon.m-v1:0"), workloads=[SHORT],
        sweep=SweepConfig(type="rate", values=[40.0]), slo=SloConfig(latency_p95_ms=3000),
        duration_s=0.1, warmup_s=0.02, stream=False, seed=1, token_counting=token_counting,
    )
    defaults.update(overrides)
    return ExperimentSpec(**defaults)


class StrategySelectionTests(unittest.TestCase):
    def test_count_tokens_is_preferred_when_the_model_supports_it(self):
        target = _target(chars_per_token=3.0, count_tokens_supported=True)
        cal = calibrate_workloads(_spec(), target)["short"]
        self.assertEqual(cal.method, "count_tokens")
        self.assertTrue(cal.converged)
        self.assertLessEqual(abs(cal.counted_input_tokens - 512), 512 * 0.02)
        self.assertEqual(target._client.converse_calls, [])  # no inference spent calibrating

    def test_falls_back_to_converse_usage_when_count_tokens_is_unsupported(self):
        target = _target(chars_per_token=3.0, count_tokens_supported=False)
        cal = calibrate_workloads(_spec(), target)["short"]
        self.assertEqual(cal.method, "converse_usage")
        self.assertTrue(cal.converged)
        self.assertIn("count_tokens unavailable", cal.note)
        probe = target._client.converse_calls[0]
        self.assertEqual(probe["inferenceConfig"]["maxTokens"], 1)  # tiny probe, not a full request

    def test_falls_back_to_estimate_when_no_counter_is_usable(self):
        """The default fake: no CountTokens, and converse usage is a
        constant -- unresponsive, so it can't size anything."""
        cal = calibrate_workloads(_spec(), _target())["short"]
        self.assertEqual(cal.method, "estimate")
        self.assertIsNone(cal.profile.filler_chars)
        self.assertIn("don't grow with input", cal.note)

    def test_forced_strategies(self):
        both = dict(chars_per_token=3.0, count_tokens_supported=True)
        self.assertEqual(calibrate_workloads(_spec("converse_usage"), _target(**both))["short"].method, "converse_usage")
        self.assertEqual(calibrate_workloads(_spec("estimate"), _target(**both))["short"].method, "estimate")
        forced_unsupported = calibrate_workloads(_spec("count_tokens"), _target(chars_per_token=3.0))["short"]
        self.assertEqual(forced_unsupported.method, "estimate")  # forced counter unavailable -> estimate, not the other

    def test_probe_rejects_a_constant_counter(self):
        self.assertIn("don't grow", probe_counter(lambda text: 10))
        self.assertIsNone(probe_counter(len))

    def test_unknown_strategy_is_an_error(self):
        with self.assertRaises(ValueError):
            resolve_counter("magic", [])


class CalibrateProfileTests(unittest.TestCase):
    def test_converges_from_a_dense_tokenizer(self):
        """The real-world failure: the repeated filler tokenizes ~10
        chars/token, so the 4-char estimate sent ~236 tokens for 512."""
        target = _target(chars_per_token=10.0, count_tokens_supported=True)
        self.assertLess(target.count_tokens(SHORT.prompt()), 300)
        result = calibrate_profile(SHORT, target.count_tokens, "count_tokens")
        self.assertTrue(result.converged)
        self.assertLessEqual(abs(target.count_tokens(result.profile.prompt()) - 512), 512 * 0.02)
        self.assertIsNone(SHORT.filler_chars)  # input profile not mutated

    def test_unreachable_target_keeps_the_closest_attempt(self):
        target = _target(chars_per_token=3.0, count_tokens_supported=True)
        tiny = WorkloadProfile(name="tiny", input_tokens=5, output_tokens=16)
        result = calibrate_profile(tiny, target.count_tokens, "count_tokens", max_iterations=4)
        self.assertFalse(result.converged)
        self.assertLessEqual(result.profile.filler_chars, 5)
        self.assertIn("closest achievable", result.note)

    def test_counter_failing_mid_calibration_falls_back_to_estimate(self):
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) > 1:
                raise RuntimeError("throttled")
            return 100

        result = calibrate_profile(SHORT, flaky, "converse_usage")
        self.assertEqual(result.method, "estimate")
        self.assertIn("failed mid-calibration", result.note)


class CountTokensModelIdTests(unittest.TestCase):
    def test_inference_profile_id_is_retried_as_its_base_model_id(self):
        client = FakeBedrockRuntimeClient(chars_per_token=3.0, count_tokens_supported=True,
                                          count_tokens_model_ids={"amazon.nova-micro-v1:0"})
        target = BedrockConverseTarget(model_id="us.amazon.nova-micro-v1:0", client=client)
        target.count_tokens("hello")
        self.assertEqual([c["modelId"] for c in client.count_tokens_calls],
                         ["us.amazon.nova-micro-v1:0", "amazon.nova-micro-v1:0"])

    def test_non_profile_model_id_is_not_stripped(self):
        client = FakeBedrockRuntimeClient(chars_per_token=3.0, count_tokens_supported=True, count_tokens_model_ids=set())
        target = BedrockConverseTarget(model_id="qwen.qwen3-32b-v1:0", client=client)
        with self.assertRaises(Exception):
            target.count_tokens("hello")
        self.assertEqual(len(client.count_tokens_calls), 1)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_measured_requests_use_the_calibrated_prompt_and_the_artifact_says_how(self):
        client = FakeBedrockRuntimeClient(chars_per_token=10.0, count_tokens_supported=True)
        target = BedrockConverseTarget(model_id="us.amazon.m-v1:0", client=client)

        report = await run_experiment(_spec(), target=target)

        cal = report.calibrations["short"]
        sent = client.converse_calls[0]["messages"][0]["content"][0]["text"]
        self.assertEqual(sent, cal.profile.prompt())
        v = build_capacity_profile(report)["workload_classes"]["short"]["workload_validation"]
        self.assertEqual(v["token_counting"]["method"], "count_tokens")
        self.assertTrue(v["token_counting"]["converged"])
        self.assertTrue(v["input"]["valid"])  # fake reports the calibrated ~512, not ~236

    async def test_calibration_probes_are_not_measured_results(self):
        client = FakeBedrockRuntimeClient(chars_per_token=3.0)  # -> converse_usage probes
        target = BedrockConverseTarget(model_id="us.amazon.m-v1:0", client=client)

        report = await run_experiment(_spec(), target=target)

        probes = [c for c in client.converse_calls if c["inferenceConfig"]["maxTokens"] == 1]
        self.assertTrue(probes)
        self.assertEqual(len(report.all_results), len(client.converse_calls) - len(probes))


if __name__ == "__main__":
    unittest.main()
