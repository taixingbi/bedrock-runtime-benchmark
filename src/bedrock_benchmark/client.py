"""BedrockConverseTarget -- calls Bedrock's Converse/ConverseStream API
directly, boto3, no gateway/API Gateway/authz/admission-control in the
path at all. That's the entire point of this repo (see README): a
number measured here is the MODEL's own capacity, not the platform's --
mixing the two in one measurement makes neither answerable.

boto3's bedrock-runtime client is synchronous; wrapped in
asyncio.to_thread so ConcurrencyRunner/RateRunner can hold many calls
in flight without blocking the event loop.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .results import RequestResult


@dataclass
class InvokeRequest:
    prompt: str
    max_tokens: int
    temperature: float = 0.0
    stream: bool = True
    # When the runner INTENDED to fire this (see RequestResult's own
    # docstring on why this is kept distinct from when it actually
    # started) -- defaults to "now" for a caller that doesn't schedule
    # ahead (e.g. a one-off manual invoke()).
    scheduled_at: float = 0.0


def _client_error_code(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return response.get("Error", {}).get("Code")


class BedrockConverseTarget:
    def __init__(self, *, model_id: str, region: str = "us-east-1", client: Optional[Any] = None):
        self.model_id = model_id
        self.region = region
        if client is not None:
            self._client = client
        else:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=region)

    async def invoke(self, request: InvokeRequest) -> RequestResult:
        return await asyncio.to_thread(self._invoke_sync, request)

    def _invoke_sync(self, request: InvokeRequest) -> RequestResult:
        request_id = str(uuid.uuid4())
        scheduled_at = request.scheduled_at or time.time()
        started_at = time.time()
        messages = [{"role": "user", "content": [{"text": request.prompt}]}]
        inference_config = {"maxTokens": request.max_tokens, "temperature": request.temperature}

        if request.stream:
            return self._invoke_stream(request_id, request, messages, inference_config, scheduled_at, started_at)

        try:
            resp = self._client.converse(modelId=self.model_id, messages=messages, inferenceConfig=inference_config)
        except Exception as exc:  # noqa: BLE001 - a failed provider call is a real RequestResult, not a crash
            return self._failure(request_id, scheduled_at, started_at, exc)

        completed_at = time.time()
        usage = resp.get("usage") or {}
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=started_at, completed_at=completed_at,
            latency_ms=round((completed_at - started_at) * 1000, 2), success=True,
            input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
        )

    def _invoke_stream(self, request_id, request, messages, inference_config, scheduled_at, started_at) -> RequestResult:
        try:
            resp = self._client.converse_stream(modelId=self.model_id, messages=messages, inferenceConfig=inference_config)
            first_token_at: Optional[float] = None
            usage: dict = {}
            for event in resp["stream"]:
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta and first_token_at is None:
                    first_token_at = time.time()
                metadata_usage = event.get("metadata", {}).get("usage")
                if metadata_usage:
                    usage = metadata_usage
        except Exception as exc:  # noqa: BLE001 - see non-streaming branch's own note
            return self._failure(request_id, scheduled_at, started_at, exc)

        completed_at = time.time()
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=started_at, completed_at=completed_at,
            first_token_at=first_token_at,
            ttft_ms=round((first_token_at - started_at) * 1000, 2) if first_token_at is not None else None,
            latency_ms=round((completed_at - started_at) * 1000, 2), success=True,
            input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
        )

    def _failure(self, request_id: str, scheduled_at: float, started_at: float, exc: Exception) -> RequestResult:
        completed_at = time.time()
        code = _client_error_code(exc)
        # botocore's own timeout exceptions (ReadTimeoutError/
        # ConnectTimeoutError) have no .response/Error.Code at all --
        # recognized by class name instead, since importing botocore's
        # exceptions module just to isinstance-check two classes isn't
        # worth it for a single boolean.
        timed_out = type(exc).__name__ in ("ReadTimeoutError", "ConnectTimeoutError")
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=started_at, completed_at=completed_at,
            latency_ms=round((completed_at - started_at) * 1000, 2), success=False,
            error=str(exc), error_code=code, throttled=(code == "ThrottlingException"), timed_out=timed_out,
        )
