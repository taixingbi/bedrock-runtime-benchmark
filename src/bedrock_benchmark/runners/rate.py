"""RateRunner -- open-loop Poisson arrival at a fixed offered rate for
duration_s, independent of how long any individual response takes
(unlike ConcurrencyRunner's closed-loop "wait for a reply before firing
the next" model). This is "what does the model do at exactly N
requests/sec offered" -- the question that finds where offered load
starts queueing/throttling, distinct from concurrency's "what does N
simultaneous in-flight calls do."
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import List, Optional

from ..client import BedrockConverseTarget, InvokeRequest
from ..results import RequestResult
from ..workload import WorkloadProfile


class RateRunner:
    def __init__(self, target: BedrockConverseTarget, profile: WorkloadProfile, *,
                 rps: float, duration_s: float, stream: bool = True, seed: Optional[int] = None):
        self._target = target
        self._profile = profile
        self._rps = rps
        self._duration_s = duration_s
        self._stream = stream
        self._rng = random.Random(seed)

    def _offsets(self) -> List[float]:
        if self._rps <= 0:
            return []
        offsets = []
        t = 0.0
        while True:
            t += self._rng.expovariate(self._rps)
            if t >= self._duration_s:
                break
            offsets.append(t)
        return offsets

    async def run(self) -> List[RequestResult]:
        offsets = self._offsets()
        # wall_t0 anchors offsets to real epoch time at the SAME instant
        # as t0's perf_counter baseline -- scheduled_at must be the
        # intended arrival time (wall_t0 + offset), computed BEFORE any
        # sleep, not time.time() sampled after waking up. Recording it
        # post-sleep (the original bug here) makes scheduled_at drift
        # to ~= started_at, destroying the one signal this field exists
        # for: client-side scheduling lag (started_at - scheduled_at)
        # when the load generator itself falls behind its own arrival
        # schedule under high offered rate.
        wall_t0 = time.time()
        t0 = time.perf_counter()

        async def fire(offset: float) -> RequestResult:
            scheduled_at = wall_t0 + offset
            delay = offset - (time.perf_counter() - t0)
            if delay > 0:
                await asyncio.sleep(delay)
            request = InvokeRequest(
                prompt=self._profile.prompt(), max_tokens=self._profile.output_tokens,
                stream=self._stream, scheduled_at=scheduled_at,
            )
            return await self._target.invoke(request)

        return list(await asyncio.gather(*(fire(o) for o in offsets)))
