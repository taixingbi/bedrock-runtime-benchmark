import unittest

from bedrock_benchmark.client import BedrockConverseTarget, InvokeRequest

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


class ScheduledAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_at_is_preserved_from_the_request(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False, scheduled_at=12345.0))

        self.assertEqual(result.scheduled_at, 12345.0)


if __name__ == "__main__":
    unittest.main()
