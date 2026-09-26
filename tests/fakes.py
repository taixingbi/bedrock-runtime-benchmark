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
                 error: Optional[Exception] = None, fail_after: Optional[int] = None,
                 chars_per_token: Optional[float] = None, count_tokens_supported: bool = False,
                 count_tokens_model_ids: Optional[set] = None):
        self._responses = responses or [{"input_tokens": 10, "output_tokens": 5, "text": "ok"}]
        self._stream_events = stream_events
        self._error = error
        self._fail_after = fail_after
        # A tokenizer for the fake: when set, converse usage.inputTokens and
        # count_tokens are computed from the prompt (a deliberately
        # non-4 ratio, so calibration has real work to do); when None,
        # converse returns the fixed template counts.
        self._chars_per_token = chars_per_token
        self._count_tokens_supported = count_tokens_supported
        # When set, only these modelIds are accepted by count_tokens.
        self._count_tokens_model_ids = count_tokens_model_ids
        self.count_tokens_calls: List[dict] = []
        self.converse_calls: List[dict] = []
        self.converse_stream_calls: List[dict] = []

    def _maybe_raise(self, calls: List[dict]) -> None:
        if self._error is not None and (self._fail_after is None or len(calls) > self._fail_after):
            raise self._error

    def converse(self, **kwargs) -> dict:
        self.converse_calls.append(kwargs)
        self._maybe_raise(self.converse_calls)
        template = self._responses[min(len(self.converse_calls) - 1, len(self._responses) - 1)]
        input_tokens = template["input_tokens"]
        if self._chars_per_token is not None:
            input_tokens = self._tokens(kwargs["messages"])
        return {
            "output": {"message": {"content": [{"text": template["text"]}]}},
            "usage": {"inputTokens": input_tokens, "outputTokens": template["output_tokens"]},
        }

    def _tokens(self, messages: list) -> int:
        text = messages[0]["content"][0]["text"]
        return int(len(text) / self._chars_per_token) + 7  # + fixed per-message overhead

    def count_tokens(self, *, modelId: str, input: dict) -> dict:
        self.count_tokens_calls.append({"modelId": modelId, "input": input})
        if not self._count_tokens_supported or self._chars_per_token is None or (
            self._count_tokens_model_ids is not None and modelId not in self._count_tokens_model_ids
        ):
            err = Exception("CountTokens is not supported for this model")
            err.response = {"Error": {"Code": "ValidationException", "Message": str(err)}}
            raise err
        return {"inputTokens": self._tokens(input["converse"]["messages"])}

    def converse_stream(self, **kwargs) -> dict:
        self.converse_stream_calls.append(kwargs)
        self._maybe_raise(self.converse_stream_calls)
        events = self._stream_events or [
            {"contentBlockDelta": {"delta": {"text": "hel"}}},
            {"contentBlockDelta": {"delta": {"text": "lo"}}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
        ]
        return {"stream": iter(events)}
