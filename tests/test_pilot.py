import unittest

from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.constraints import SloConfig
from bedrock_benchmark.experiments.schema import load_experiment
from bedrock_benchmark.models import ModelConfig
from bedrock_benchmark.pilot import FAIL, OK, WARN, evaluate, pilot_workloads, run_pilot_sync
from bedrock_benchmark.results import RequestResult
from bedrock_benchmark.workload import WorkloadProfile

from .fakes import FakeBedrockRuntimeClient

MICRO = ModelConfig(name="nova-micro", model_id="us.amazon.nova-micro-v1:0", quota_rpm=400, quota_tpm=8_000_000)
SPEC = load_experiment("experiments/rate-capacity.yaml", MICRO)
CHAT = WorkloadProfile(name="short_chat", input_tokens=512, output_tokens=64, slo_profile="gold", latency_p95_ms=3000)
GOLD = SloConfig(ttft_p95_ms=800, tpot_p95_ms=40, latency_p95_ms=3000, success_rate_min=0.995, throttle_rate_max=0.001)


def _r(**kw) -> RequestResult:
    base = dict(request_id="r", scheduled_at=0.0, started_at=0.0, completed_at=0.6, success=True,
                ttft_ms=400.0, latency_ms=400.0 + 63 * 4.0, input_tokens=508, output_tokens=64,
                stop_reason="max_tokens")
    base.update(kw)
    return RequestResult(**base)


class EvaluateTests(unittest.TestCase):
    def test_healthy_workload_is_ok(self):
        c = evaluate("m", CHAT, GOLD, [_r(), _r(), _r()], SPEC, "converse_usage")
        self.assertEqual((c.status, c.issues), (OK, []))
        self.assertEqual((c.output_p50, c.max_tokens_share, c.tpot_p50_ms), (64, 1.0, 4.0))

    def test_access_errors_fail(self):
        bad = _r(success=False, error_code="AccessDeniedException", output_tokens=None)
        c = evaluate("m", CHAT, GOLD, [bad, bad, bad], SPEC, None)
        self.assertEqual(c.status, FAIL)
        self.assertIn("AccessDeniedException", " ".join(c.issues))

    def test_short_output_fails_the_shape_check(self):
        """The bug the prompt fix addressed: 32 of 64 tokens, end_turn."""
        c = evaluate("m", CHAT, GOLD, [_r(output_tokens=32, stop_reason="end_turn")] * 3, SPEC, None)
        self.assertEqual(c.status, FAIL)
        self.assertIn("output p50 32 vs 64", " ".join(c.issues))

    def test_off_target_input_fails(self):
        c = evaluate("m", CHAT, GOLD, [_r(input_tokens=236)] * 3, SPEC, None)
        self.assertEqual(c.status, FAIL)

    def test_unloaded_latency_over_the_slo_warns_not_fails(self):
        c = evaluate("m", CHAT, GOLD, [_r(ttft_ms=900.0, latency_ms=900.0 + 63 * 4.0)] * 3, SPEC, None)
        self.assertEqual(c.status, WARN)
        self.assertIn("TTFT", " ".join(c.issues))

    def test_a_throttle_at_one_request_at_a_time_warns(self):
        throttled = _r(success=False, error_code="ThrottlingException", throttled=True)
        c = evaluate("m", CHAT, GOLD, [_r(), _r(), throttled], SPEC, None)
        self.assertEqual(c.status, WARN)


class PilotRunTests(unittest.TestCase):
    def test_union_of_planned_workloads_respects_the_slo_filter(self):
        paths = ["experiments/rate-capacity.yaml", "experiments/token-sweep.yaml", "experiments/mixed-capacity.yaml"]
        all_ = pilot_workloads(paths, MICRO)
        self.assertEqual([w.name for w in all_.workloads],
                         ["short_chat", "rag_answer", "long_generation", "long_context_short_answer"])
        gold = pilot_workloads(paths, MICRO, only_slo_profiles={"gold"})
        self.assertEqual([w.name for w in gold.workloads], ["short_chat"])

    def test_run_pilot_flags_the_fake_clients_short_output(self):
        """The fake returns 5 output tokens without a stop reason -- the
        pilot must catch that before any long run."""
        factory = lambda spec: BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        report = run_pilot_sync(["experiments/concurrency-sweep.yaml"], [MICRO], requests_per_workload=2,
                                target_factory=factory)
        [check] = report.checks
        self.assertEqual((check.workload, check.requests, check.status), ("short_chat", 2, FAIL))
        self.assertEqual(report.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
