import asyncio
import time as time_module
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


    async def test_window_excludes_warmup_and_drain_is_recorded(self):
        class SlowFakeTarget(BedrockConverseTarget):
            async def invoke(self, request):
                await asyncio.sleep(0.05)
                return await super().invoke(request)

        target = SlowFakeTarget(model_id="m", client=FakeBedrockRuntimeClient())
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = ConcurrencyRunner(target, profile, concurrency=2, duration_s=0.2, warmup_s=0.1, stream=False)

        results = await runner.run()

        self.assertAlmostEqual(runner.window.duration_s, 0.2, places=6)
        self.assertTrue(any(r.scheduled_at < runner.window.start for r in results))  # warmup ran
        self.assertTrue(any(r.completed_at >= runner.window.end for r in results))   # drain recorded
        self.assertTrue(all(r.scheduled_at < runner.window.end for r in results))    # nothing fired after close


class RateRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrivals_span_warmup_plus_window(self):
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient())
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        runner = RateRunner(target, profile, rps=50.0, duration_s=0.3, warmup_s=0.2, stream=False, seed=0)

        results = await runner.run()

        self.assertTrue(any(r.scheduled_at < runner.window.start for r in results))
        self.assertTrue(any(runner.window.contains(r.scheduled_at) for r in results))
        self.assertTrue(all(r.scheduled_at < runner.window.end for r in results))

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

    async def test_scheduled_at_reflects_the_intended_arrival_time_not_post_sleep_dispatch(self):
        """The real bug: scheduled_at used to be set to time.time()
        AFTER the runner's own asyncio.sleep(delay), making it drift to
        ~= started_at and destroying the one signal it exists for --
        client-side scheduling lag under a rate the load generator
        itself can't keep up with. Verified here by using a rate high
        enough, and a slow enough fake client, that real queueing MUST
        occur -- if scheduled_at were captured post-sleep, it would
        track started_at almost exactly instead of the intended,
        strictly-increasing arrival schedule."""
        class SlowFakeTarget(BedrockConverseTarget):
            async def invoke(self, request):
                await asyncio.sleep(0.05)  # slower than the offered rate, forces queueing
                return await super().invoke(request)

        client = FakeBedrockRuntimeClient()
        target = SlowFakeTarget(model_id="m", client=client)
        profile = WorkloadProfile(name="short", input_tokens=100, output_tokens=16)
        run_start = time_module.time()
        runner = RateRunner(target, profile, rps=50.0, duration_s=0.3, stream=False, seed=0)

        results = await runner.run()

        # Every result's scheduled_at must fall within the run's own
        # wall-clock window -- a real, sane timestamp, not garbage.
        self.assertTrue(all(run_start - 1.0 <= r.scheduled_at <= run_start + 5.0 for r in results))
        # scheduled_at values must be strictly non-decreasing and
        # span close to the full duration_s -- if they were captured
        # post-sleep (all serialized behind the slow fake target),
        # they'd instead cluster near the END of the run, compressed
        # into a much narrower window than the intended arrival spread.
        scheduled_ats = [r.scheduled_at for r in results]
        self.assertEqual(scheduled_ats, sorted(scheduled_ats))
        spread = scheduled_ats[-1] - scheduled_ats[0]
        self.assertGreater(spread, 0.15)  # intended arrivals really do spread across ~0.3s


if __name__ == "__main__":
    unittest.main()
