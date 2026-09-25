"""ConcurrencyRunner -- fixed N closed-loop workers, each firing a
request, waiting for its response, then immediately firing the next,
for duration_s. This is "what does the model do at exactly concurrency
C" -- the standard way to find a capacity knee (throughput/TTFT/error
rate as a function of concurrency), distinct from RateRunner's
open-loop "what does the model do at exactly N requests/sec" question.
Closed-loop is deliberate here: an open-loop generator at a fixed rate
would just queue up behind a slow backend rather than reveal what a
given concurrency level alone does to latency.
"""
from __future__ import annotations

import asyncio
import time
from typing import List

from ..client import BedrockConverseTarget, InvokeRequest
from ..results import RequestResult
from ..workload import WorkloadProfile


class ConcurrencyRunner:
    def __init__(self, target: BedrockConverseTarget, profile: WorkloadProfile, *,
                 concurrency: int, duration_s: float, stream: bool = True):
        self._target = target
        self._profile = profile
        self._concurrency = concurrency
        self._duration_s = duration_s
        self._stream = stream

    async def run(self) -> List[RequestResult]:
        end_at = time.perf_counter() + self._duration_s
        results: List[RequestResult] = []
        lock = asyncio.Lock()

        async def worker() -> None:
            while time.perf_counter() < end_at:
                request = InvokeRequest(
                    prompt=self._profile.prompt(), max_tokens=self._profile.output_tokens,
                    stream=self._stream, scheduled_at=time.time(),
                )
                result = await self._target.invoke(request)
                async with lock:
                    results.append(result)

        await asyncio.gather(*(worker() for _ in range(self._concurrency)))
        return results
