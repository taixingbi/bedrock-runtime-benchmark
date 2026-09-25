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
        """The whole point: retry_max_attempts=1 by default, so a real
        Bedrock throttle is observed and recorded, never silently
        absorbed by the SDK's own retry into an eventual success (see
        TransportConfig's own docstring)."""
        self.assertEqual(TransportConfig().retry_max_attempts, 1)

    def test_real_client_construction_applies_transport_config(self):
        """Without an injected fake client, BedrockConverseTarget must
        build its own boto3 client with an explicit botocore Config
        reflecting the given TransportConfig -- not boto3's implicit
        defaults (unbounded-ish pooling, automatic retries) that would
        make a concurrency sweep measure the SDK, not Bedrock."""
        transport = TransportConfig(max_connections=32, retry_max_attempts=3, connect_timeout_s=2.0, read_timeout_s=30.0)
        with patch("boto3.client") as mock_boto_client:
            mock_boto_client.return_value = MagicMock()
            BedrockConverseTarget(model_id="m", region="us-west-2", transport=transport)

        self.assertEqual(mock_boto_client.call_count, 1)
        _, kwargs = mock_boto_client.call_args
        config = kwargs["config"]
        self.assertEqual(config.max_pool_connections, 32)
        self.assertEqual(config.connect_timeout, 2.0)
        self.assertEqual(config.read_timeout, 30.0)
        self.assertEqual(config.retries["max_attempts"], 3)


class ScheduledAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_at_is_preserved_from_the_request(self):
        client = FakeBedrockRuntimeClient()
        target = BedrockConverseTarget(model_id="m", client=client)

        result = await target.invoke(InvokeRequest(prompt="hello", max_tokens=64, stream=False, scheduled_at=12345.0))

        self.assertEqual(result.scheduled_at, 12345.0)


if __name__ == "__main__":
    unittest.main()
