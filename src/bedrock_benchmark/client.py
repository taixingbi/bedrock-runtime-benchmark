"""BedrockConverseTarget -- calls Bedrock's Converse/ConverseStream API
directly, boto3, no gateway/API Gateway/authz/admission-control in the
path at all. That's the entire point of this repo (see README): a
number measured here is the MODEL's own capacity, not the platform's --
mixing the two in one measurement makes neither answerable.

boto3's bedrock-runtime client is synchronous, so calls run on a
thread pool the target OWNS -- not asyncio.to_thread's default
executor, which is min(32, cpu_count + 4) workers (14 on a 10-core
laptop). Past that many in-flight calls, requests would silently queue
for a Python thread and the benchmark would measure the thread pool,
not Bedrock -- the same contamination as boto3's default 10-connection
pool. The pool is sized from TransportConfig (executor_workers >=
max_connections), and the target tracks peak outstanding calls so a
point that ever exceeded the pool is flagged client_limited instead of
silently reported.

Latency and TTFT are measured with time.perf_counter() (monotonic).
Wall-clock timestamps (started_at/first_token_at/completed_at) are kept
for audit and window membership, derived from ONE wall-clock anchor
plus monotonic deltas, so an NTP/clock adjustment mid-request can't
distort a measured duration.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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


@dataclass
class TransportConfig:
    """Explicit boto3 networking/retry config -- NOT the SDK's implicit
    defaults. Two failure modes this exists to prevent:

    1. Connection-pool/thread-pool exhaustion masquerading as model
       capacity: sweeping concurrency up to 32/64 against boto3's
       default pool size (10) would measure the SDK's own queueing,
       not Bedrock's.
    2. The SDK's own automatic retry silently absorbing a real
       ThrottlingException (Bedrock 429 -> SDK retries -> eventual 200
       -> this repo records "success") -- which UNDERSTATES the real
       throttle rate, exactly the number this repo exists to measure
       accurately. total_max_attempts=1 (the default here) means
       exactly one attempt, no SDK-level retries: every throttle is
       observed and recorded, not silently absorbed.

       This MUST be passed to botocore as `total_max_attempts`, not
       `max_attempts`: botocore's own client-config normalization
       (botocore/args.py, _compute_retry_max_attempts) treats a
       `max_attempts` key as meaning *retry* attempts and silently
       rewrites it to `total_max_attempts = max_attempts + 1` before
       building the retry handler. So `max_attempts=1` actually means
       1 initial request + 1 retry = 2 total attempts -- a real 429
       could still get silently retried into a 200, exactly the
       failure mode this config exists to prevent. `total_max_attempts`
       has no such off-by-one: it is the literal total attempt count.

    Recorded into the capacity-profile.yaml artifact (see report.py)
    so a measurement is reproducible -- what was actually running when
    a number was measured, not just the number itself.
    """
    max_connections: int = 64
    total_max_attempts: int = 1  # 1 = exactly one attempt, no retries; see docstring above on why not `max_attempts`
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 60.0
    # Threads running blocking boto3 calls. None = max_connections. Must
    # be >= max_connections, or connections beyond the thread count are
    # unreachable and the pool silently becomes the bottleneck.
    executor_workers: Optional[int] = None

    def __post_init__(self) -> None:
        if self.executor_workers is not None and self.executor_workers < self.max_connections:
            raise ValueError(
                f"transport.executor_workers ({self.executor_workers}) must be >= max_connections "
                f"({self.max_connections}) -- otherwise the thread pool caps in-flight calls below the connection pool"
            )

    @property
    def effective_executor_workers(self) -> int:
        return self.executor_workers if self.executor_workers is not None else self.max_connections


def _client_error_code(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return response.get("Error", {}).get("Code")


class _Clock:
    """One wall-clock anchor + a monotonic baseline taken together:
    durations come from perf_counter (immune to clock adjustments),
    wall timestamps are anchor + monotonic delta (consistent with each
    other and with the measured durations)."""

    def __init__(self) -> None:
        self.wall_start = time.time()
        self._mono_start = time.perf_counter()

    def elapsed(self) -> float:
        return time.perf_counter() - self._mono_start

    def wall_at(self, elapsed: float) -> float:
        return self.wall_start + elapsed


class BedrockConverseTarget:
    def __init__(self, *, model_id: str, region: str = "us-east-1", client: Optional[Any] = None,
                 transport: Optional[TransportConfig] = None):
        self.model_id = model_id
        self.region = region
        self.transport = transport or TransportConfig()
        if client is not None:
            self._client = client
        else:
            import boto3
            from botocore.config import Config as BotoConfig

            boto_config = BotoConfig(
                max_pool_connections=self.transport.max_connections,
                connect_timeout=self.transport.connect_timeout_s,
                read_timeout=self.transport.read_timeout_s,
                retries={"total_max_attempts": self.transport.total_max_attempts, "mode": "standard"},
            )
            self._client = boto3.client("bedrock-runtime", region_name=region, config=boto_config)
        self.executor_workers = self.transport.effective_executor_workers
        self._executor = ThreadPoolExecutor(max_workers=self.executor_workers, thread_name_prefix="bedrock-bench")
        # Outstanding = submitted to the pool and not yet finished (running
        # OR waiting for a thread). Only touched on the event-loop thread,
        # so no lock is needed.
        self._outstanding = 0
        self.peak_outstanding = 0

    def reset_peak(self) -> None:
        self.peak_outstanding = self._outstanding

    @property
    def client_limited(self) -> bool:
        """True if calls ever queued for a thread since the last
        reset_peak() -- the measurement then includes client-side
        queueing and isn't a clean Bedrock measurement."""
        return self.peak_outstanding > self.executor_workers

    def close(self) -> None:
        self._executor.shutdown(wait=False)

    async def invoke(self, request: InvokeRequest) -> RequestResult:
        loop = asyncio.get_running_loop()
        self._outstanding += 1
        self.peak_outstanding = max(self.peak_outstanding, self._outstanding)
        try:
            return await loop.run_in_executor(self._executor, self._invoke_sync, request)
        finally:
            self._outstanding -= 1

    def _invoke_sync(self, request: InvokeRequest) -> RequestResult:
        request_id = str(uuid.uuid4())
        scheduled_at = request.scheduled_at or time.time()
        clock = _Clock()
        started_at = clock.wall_start
        messages = [{"role": "user", "content": [{"text": request.prompt}]}]
        inference_config = {"maxTokens": request.max_tokens, "temperature": request.temperature}

        if request.stream:
            return self._invoke_stream(request_id, request, messages, inference_config, scheduled_at, clock)

        try:
            resp = self._client.converse(modelId=self.model_id, messages=messages, inferenceConfig=inference_config)
        except Exception as exc:  # noqa: BLE001 - a failed provider call is a real RequestResult, not a crash
            return self._failure(request_id, scheduled_at, clock, exc)

        elapsed = clock.elapsed()
        usage = resp.get("usage") or {}
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=started_at,
            completed_at=clock.wall_at(elapsed),
            latency_ms=round(elapsed * 1000, 2), success=True,
            input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
        )

    def _invoke_stream(self, request_id, request, messages, inference_config, scheduled_at, clock: "_Clock") -> RequestResult:
        try:
            resp = self._client.converse_stream(modelId=self.model_id, messages=messages, inferenceConfig=inference_config)
            first_token_elapsed: Optional[float] = None
            usage: dict = {}
            for event in resp["stream"]:
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta and first_token_elapsed is None:
                    first_token_elapsed = clock.elapsed()
                metadata_usage = event.get("metadata", {}).get("usage")
                if metadata_usage:
                    usage = metadata_usage
        except Exception as exc:  # noqa: BLE001 - see non-streaming branch's own note
            return self._failure(request_id, scheduled_at, clock, exc)

        elapsed = clock.elapsed()
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=clock.wall_start,
            completed_at=clock.wall_at(elapsed),
            first_token_at=clock.wall_at(first_token_elapsed) if first_token_elapsed is not None else None,
            ttft_ms=round(first_token_elapsed * 1000, 2) if first_token_elapsed is not None else None,
            latency_ms=round(elapsed * 1000, 2), success=True,
            input_tokens=usage.get("inputTokens"), output_tokens=usage.get("outputTokens"),
        )

    def _failure(self, request_id: str, scheduled_at: float, clock: "_Clock", exc: Exception) -> RequestResult:
        elapsed = clock.elapsed()
        code = _client_error_code(exc)
        # botocore's own timeout exceptions (ReadTimeoutError/
        # ConnectTimeoutError) have no .response/Error.Code at all --
        # recognized by class name instead, since importing botocore's
        # exceptions module just to isinstance-check two classes isn't
        # worth it for a single boolean.
        timed_out = type(exc).__name__ in ("ReadTimeoutError", "ConnectTimeoutError")
        return RequestResult(
            request_id=request_id, scheduled_at=scheduled_at, started_at=clock.wall_start,
            completed_at=clock.wall_at(elapsed),
            latency_ms=round(elapsed * 1000, 2), success=False,
            error=str(exc), error_code=code, throttled=(code == "ThrottlingException"), timed_out=timed_out,
        )
