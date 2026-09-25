"""A hand-rolled fake boto3 bedrock-runtime client -- this repo's whole
point is measuring real Bedrock behavior, so nothing here fakes
Bedrock's OWN behavior beyond what's needed to test this repo's own
client/runner/analysis code without spending real calls in unit tests.
"""
from __future__ import annotations

from typing import List, Optional


class ThrottlingError(Exception):
    def __init__(self, message: str = "throttled"):
        super().__init__(message)
        self.response = {"Error": {"Code": "ThrottlingException", "Message": message}}


class FakeBedrockRuntimeClient:
    def __init__(self, *, responses: Optional[List[dict]] = None, stream_events: Optional[List[dict]] = None,
                 error: Optional[Exception] = None, fail_after: Optional[int] = None):
        self._responses = responses or [{"input_tokens": 10, "output_tokens": 5, "text": "ok"}]
        self._stream_events = stream_events
        self._error = error
        self._fail_after = fail_after
        self.converse_calls: List[dict] = []
        self.converse_stream_calls: List[dict] = []

    def _maybe_raise(self, calls: List[dict]) -> None:
        if self._error is not None and (self._fail_after is None or len(calls) > self._fail_after):
            raise self._error

    def converse(self, **kwargs) -> dict:
        self.converse_calls.append(kwargs)
        self._maybe_raise(self.converse_calls)
        template = self._responses[min(len(self.converse_calls) - 1, len(self._responses) - 1)]
        return {
            "output": {"message": {"content": [{"text": template["text"]}]}},
            "usage": {"inputTokens": template["input_tokens"], "outputTokens": template["output_tokens"]},
        }

    def converse_stream(self, **kwargs) -> dict:
        self.converse_stream_calls.append(kwargs)
        self._maybe_raise(self.converse_stream_calls)
        events = self._stream_events or [
            {"contentBlockDelta": {"delta": {"text": "hel"}}},
            {"contentBlockDelta": {"delta": {"text": "lo"}}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
        ]
        return {"stream": iter(events)}
