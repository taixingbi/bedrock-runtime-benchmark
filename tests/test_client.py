import unittest
from unittest.mock import MagicMock, patch

from bedrock_benchmark.client import BedrockConverseTarget, InvokeRequest, TransportConfig

from .fakes import FakeBedrockRuntimeClient, ThrottlingError


class NonStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_records_real_token_counts_and_latency(self):
        client = FakeBedrockRuntimeClient(responses=[{"input_tokens": 128, "output_tokens": 32, "text": "hi"}])
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False))

        self.assertTrue(result.success)
        self.assertEqual(result.input_tokens, 128)
        self.assertEqual(result.output_tokens, 32)
        self.assertIsNotNone(result.latency_ms)
        self.assertIsNone(result.ttft_ms)  # non-streaming -- no TTFT concept

    async def test_throttling_is_a_failed_result_not_an_exception(self):
        client = FakeBedrockRuntimeClient(error=ThrottlingError())
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False))

        self.assertFalse(result.success)
        self.assertTrue(result.throttled)
        self.assertEqual(result.error_code, "ThrottlingException")

    async def test_non_throttling_error_is_not_marked_throttled(self):
        client = FakeBedrockRuntimeClient(error=RuntimeError("boom"))
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False))

        self.assertFalse(result.success)
        self.assertFalse(result.throttled)
        self.assertIsNone(result.error_code)  # not a real boto ClientError


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_reason_is_recorded(self):
        """max_tokens = the output budget was used; end_turn = the model
        stopped early, below the workload's output target."""
        for reason in ("max_tokens", "end_turn"):
            client = FakeBedrockRuntimeClient(stream_events=[
                {"contentBlockDelta": {"delta": {"text": "a"}}},
                {"messageStop": {"stopReason": reason}},
                {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 10}}},
            ])
            target = BedrockConverseTarget(model_id="m", client=client)
            result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=10, stream=True))
            self.assertEqual(result.stop_reason, reason)
            target.close()

    async def test_ttft_is_time_of_first_delta_and_usage_comes_from_metadata(self):
        client = FakeBedrockRuntimeClient(stream_events=[
            {"contentBlockDelta": {"delta": {"text": "a"}}},
            {"contentBlockDelta": {"delta": {"text": "b"}}},
            {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 10}}},
        ])
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=True))

        self.assertTrue(result.success)
        self.assertIsNotNone(result.ttft_ms)
        self.assertEqual(result.input_tokens, 20)
        self.assertEqual(result.output_tokens, 10)

    async def test_streaming_throttling_still_sets_ttft_none_if_no_tokens_arrived(self):
        client = FakeBedrockRuntimeClient(error=ThrottlingError())
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=True))

        self.assertFalse(result.success)
        self.assertTrue(result.throttled)
        self.assertIsNone(result.ttft_ms)


class TransportConfigTests(unittest.TestCase):
    def test_defaults_disable_sdk_retries(self):
        """The whole point: total_max_attempts=1 by default, so a real
        Bedrock throttle is observed and recorded, never silently
        absorbed by the SDK's own retry into an eventual success (see
        TransportConfig's own docstring). Must be `total_max_attempts`,
        not `max_attempts` -- botocore's client-config normalization
        rewrites a `max_attempts` key to `total_max_attempts + 1`,
        which would silently allow one retry even at the value meant
        to mean "no retries"."""
        self.assertEqual(TransportConfig().total_max_attempts, 1)

    def test_real_client_construction_applies_transport_config(self):
        """Without an injected fake client, BedrockConverseTarget must
        build its own boto3 client with an explicit botocore Config
        reflecting the given TransportConfig -- not boto3's implicit
        defaults (unbounded-ish pooling, automatic retries) that would
        make a concurrency sweep measure the SDK, not Bedrock."""
        transport = TransportConfig(max_connections=32, total_max_attempts=3, connect_timeout_s=2.0, read_timeout_s=30.0)
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = MagicMock()
            BedrockConverseTarget(model_id="m", region="us-west-2", transport=transport)

        self.assertEqual(mock_boto_client.call_count, 1)
        _, kwargs = mock_boto_client.call_args
        config = kwargs["config"]
        self.assertEqual(config.max_pool_connections, 32)
        self.assertEqual(config.connect_timeout, 2.0)
        self.assertEqual(config.read_timeout, 30.0)
        self.assertEqual(config.retries["total_max_attempts"], 3)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """The P0: asyncio.to_thread's default pool is min(32, cpu+4) --
    14 on a 10-core laptop -- so beyond that many in-flight calls the
    benchmark would silently measure Python thread queueing."""

    def test_executor_defaults_to_max_connections(self):
        target = BedrockConverseTarget(model_id="m", client=FakeBedrockRuntimeClient(),
                                       transport=TransportConfig(max_connections=48))
        self.assertEqual(target.executor_workers, 48)
        self.assertEqual(target._executor._max_workers, 48)
        target.close()

    def test_executor_smaller_than_connection_pool_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "executor_workers"):
            TransportConfig(max_connections=64, executor_workers=16)

    async def test_all_calls_run_concurrently_up_to_the_pool_size(self):
        import asyncio
        import threading
        import time as time_module

        barrier = threading.Barrier(20, timeout=5)

        class BarrierClient(FakeBedrockRuntimeClient):
            def converse(self, **kwargs):
                barrier.wait()  # deadlocks (BrokenBarrierError) unless 20 threads run at once
                return super().converse(**kwargs)

        target = BedrockConverseTarget(model_id="m", client=BarrierClient(),
                                       transport=TransportConfig(max_connections=20))
        start = time_module.perf_counter()
        results = await asyncio.gather(*(target.invoke(InvokeRequest(prompt="p", max_tokens=4, stream=False))
                                         for _ in range(20)))
        self.assertTrue(all(r.success for r in results))
        self.assertLess(time_module.perf_counter() - start, 5)
        self.assertEqual(target.peak_outstanding, 20)
        self.assertFalse(target.client_limited)
        target.close()

    async def test_more_outstanding_calls_than_threads_is_flagged_client_limited(self):
        import asyncio

        class SlowClient(FakeBedrockRuntimeClient):
            def converse(self, **kwargs):
                import time as t
                t.sleep(0.02)
                return super().converse(**kwargs)

        target = BedrockConverseTarget(model_id="m", client=SlowClient(), transport=TransportConfig(max_connections=2))
        await asyncio.gather(*(target.invoke(InvokeRequest(prompt="p", max_tokens=4, stream=False)) for _ in range(5)))
        self.assertEqual(target.peak_outstanding, 5)
        self.assertTrue(target.client_limited)
        target.reset_peak()
        self.assertFalse(target.client_limited)
        target.close()


class MonotonicClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_wall_clock_jump_does_not_distort_latency_or_ttft(self):
        """time.time() jumps back an hour mid-request (NTP correction);
        durations come from perf_counter, so they stay correct."""
        import time as real_time

        class JumpingClient(FakeBedrockRuntimeClient):
            def converse_stream(self, **kwargs):
                real_time.sleep(0.02)
                return super().converse_stream(**kwargs)

        wall = iter([1_000_000.0] + [996_400.0] * 10)  # anchor, then -3600s for any later call
        target = BedrockConverseTarget(model_id="m", client=JumpingClient())
        with patch("bedrock_benchmark.client.time.time", side_effect=lambda: next(wall)):
            result = await target.invoke(InvokeRequest(prompt="p", max_tokens=4, stream=True, scheduled_at=1.0))

        self.assertGreater(result.latency_ms, 15)
        self.assertLess(result.latency_ms, 2000)
        self.assertGreater(result.ttft_ms, 15)
        # Wall timestamps are anchor + monotonic delta -- consistent, never negative durations.
        self.assertAlmostEqual((result.completed_at - result.started_at) * 1000, result.latency_ms, places=1)
        target.close()


class ScheduledAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_at_is_preserved_from_the_request(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False, scheduled_at=12345.0))

        self.assertEqual(result.scheduled_at, 12345.0)


if __name__ == "__main__":
    unittest.main()
