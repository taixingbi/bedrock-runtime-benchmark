import unittest

from bedrock_benchmark.client import BedrockConverseTarget
from bedrock_benchmark.runners.concurrency import ConcurrencyRunner
from bedrock_benchmark.runners.rate import RateRunner
from bedrock_benchmark.workload import WorkloadProfile

from .fakes import FakeBedrockRuntimeClient


class ConcurrencyRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fires_roughly_concurrency_many_requests_over_duration(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = ConcurrencyRunner(target, profile, concurrency=3, duration_s=0.2, stream=False)

        results = await runner.run()

        self.assertTrue(all(r.success for r in results))
        # Each of the 3 workers fires back-to-back for 0.2s -- with a
        # near-instant fake client that's "many" requests, not an exact
        # count (closed-loop timing isn't deterministic to the request).
        self.assertGreaterEqual(len(results), 3)

    async def test_zero_concurrency_produces_no_results(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = ConcurrencyRunner(target, profile, concurrency=0, duration_s=0.1, stream=False)

        results = await runner.run()
        self.assertEqual(results, [])


class RateRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fires_expected_number_of_requests_for_a_constant_ish_rate(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = RateRunner(target, profile, rps=20.0, duration_s=1.0, stream=False, seed=0)

        results = await runner.run()

        self.assertTrue(all(r.success for r in results))
        # Poisson mean count is rps*duration_s=20 -- allow real variance.
        self.assertGreater(len(results), 5)
        self.assertLess(len(results), 40)

    async def test_zero_rps_produces_no_results(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = RateRunner(target, profile, rps=0.0, duration_s=0.1, stream=False)

        results = await runner.run()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
