"""RequestResult -- one call to Bedrock, raw. Every field the analysis
layer needs is kept here, not just aggregates, because a benchmark's
whole value is being able to re-derive a DIFFERENT aggregate later
(a new percentile, a new SLO threshold, a new outcome breakdown)
without re-running real Bedrock calls -- see storage.py, which
persists a full JSONL of these per run for exactly that reason.

ttft_ms/first_token_at are None (not 0) for a non-streaming request --
"time to first token" isn't a distinct concept from e2e latency when
the whole response arrives in one shot, same convention used
throughout this project's sibling repos.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RequestResult:
    request_id: str
    # scheduled_at: when the runner INTENDED to fire this request (the
    # arrival-schedule offset, converted to wall-clock) -- distinct
    # from started_at, which is when the call actually began. Under
    # real concurrency contention these drift apart; the gap itself is
    # a real signal (client-side queueing), not noise to discard.
    scheduled_at: float
    started_at: float
    completed_at: float
    first_token_at: Optional[float] = None
    ttft_ms: Optional[float] = None
    latency_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    success: bool = True
    error: Optional[str] = None
    # The real boto3/Bedrock ClientError code (e.g. "ThrottlingException")
    # -- None on success, and also None for a failure that isn't a
    # recognized AWS ClientError (a raw network/timeout exception).
    error_code: Optional[str] = None
    throttled: bool = False
    timed_out: bool = False
    # Tags a runner/sweep adds for downstream grouping (workload name,
    # concurrency/rps value) -- kept generic rather than named fields
    # here since which sweep dimension applies varies per experiment.
    tags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkRun:
    """Metadata for one sweep POINT (one concurrency or rps value, one
    workload profile, run for its own duration_s) -- not the whole
    sweep. A sweep experiment produces one of these per point; see
    experiments/executor.py."""
    run_id: str
    experiment: str
    model_id: str
    region: str
    workload_name: str
    concurrency: Optional[int] = None
    target_rps: Optional[float] = None
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    results: List[RequestResult] = field(default_factory=list)

    @staticmethod
    def new_run_id() -> str:
        return str(uuid.uuid4())
