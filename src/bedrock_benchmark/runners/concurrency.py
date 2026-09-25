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
import random
import time
from typing import List, Optional, Union

from ..analysis.metrics import MeasurementWindow
from ..client import BedrockConverseTarget, InvokeRequest
from ..results import RequestResult
from ..workload import WorkloadMix, WorkloadProfile


class ConcurrencyRunner:
    def __init__(self, target: BedrockConverseTarget, profile: Union[WorkloadProfile, WorkloadMix], *,
                 concurrency: int, duration_s: float, stream: bool = True, warmup_s: float = 0.0,
                 seed: Optional[int] = None):
        self._target = target
        self._profile = profile
        self._concurrency = concurrency
        self._duration_s = duration_s
        self._warmup_s = warmup_s
        self._stream = stream
        self._rng = random.Random(seed)  # only used to draw classes from a WorkloadMix
        # Set by run(): the wall-clock span compute_run_metrics counts.
        # Workers keep firing only until the window closes; requests
        # still in flight then finish (drain) and are recorded, but a
        # completion after window.end never counts toward throughput.
        self.window: Optional[MeasurementWindow] = None

    async def run(self) -> List[RequestResult]:
        wall_t0 = time.time()
        end_at = time.perf_counter() + self._warmup_s + self._duration_s
        self.window = MeasurementWindow(
            start=wall_t0 + self._warmup_s, end=wall_t0 + self._warmup_s + self._duration_s,
        )
        results: List[RequestResult] = []
        lock = asyncio.Lock()

        async def worker() -> None:
            while time.perf_counter() < end_at:
                chosen = self._profile.sample(self._rng)
                request = InvokeRequest(
                    prompt=chosen.prompt(), max_tokens=chosen.output_tokens,
                    stream=self._stream, scheduled_at=time.time(),
                )
                result = await self._target.invoke(request)
                result.tags["workload"] = chosen.name
                async with lock:
                    results.append(result)

        await asyncio.gather(*(worker() for _ in range(self._concurrency)))
        return results
