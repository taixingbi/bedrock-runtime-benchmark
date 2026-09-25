"""Turns a flat list of RequestResult into the numbers capacity.py's
recommendation engine actually reads. SLO goodput -- not raw
throughput -- is the headline number here (see this repo's own
README): a config that maximizes throughput while blowing through TTFT
SLO and drawing real throttling is a worse config than one with lower
raw throughput and zero SLO violations, and only slo_goodput_rps makes
that comparison possible at a glance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..results import RequestResult


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class RunMetrics:
    n: int
    success_rate: float
    throttle_rate: float
    timeout_rate: float
    request_throughput_rps: float
    token_throughput_tps: Optional[float]  # output tokens/sec, successes only
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    ttft_p50_ms: Optional[float] = None
    ttft_p95_ms: Optional[float] = None
    ttft_p99_ms: Optional[float] = None
    # None until an SLO is actually configured -- see compute_run_metrics.
    slo_goodput_rps: Optional[float] = None
    # slo_goodput_rps / offered_rps -- "what fraction of what you asked
    # for came back both successful and within SLO." offered_rps is the
    # sweep point's target (concurrency's own achieved throughput has no
    # separate "offered" concept -- see compute_run_metrics).
    slo_efficiency: Optional[float] = None


def compute_run_metrics(
    results: List[RequestResult], *, duration_s: float,
    ttft_slo_ms: Optional[float] = None, latency_slo_ms: Optional[float] = None,
    offered_rps: Optional[float] = None,
) -> RunMetrics:
    n = len(results)
    if n == 0 or duration_s <= 0:
        return RunMetrics(
            n=0, success_rate=0.0, throttle_rate=0.0, timeout_rate=0.0,
            request_throughput_rps=0.0, token_throughput_tps=None,
            latency_p50_ms=0.0, latency_p95_ms=0.0, latency_p99_ms=0.0,
        )

    successes = [r for r in results if r.success]
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    ttfts = [r.ttft_ms for r in results if r.ttft_ms is not None]
    output_tokens = [r.output_tokens for r in successes if r.output_tokens is not None]

    slo_goodput_rps = None
    slo_efficiency = None
    if ttft_slo_ms is not None or latency_slo_ms is not None:
        def meets_slo(r: RequestResult) -> bool:
            if not r.success:
                return False
            if latency_slo_ms is not None and (r.latency_ms is None or r.latency_ms > latency_slo_ms):
                return False
            # A TTFT SLO is configured but this result has no TTFT at
            # all (non-streaming request, or a streaming measurement
            # that failed to capture one) -- that's a missing/invalid
            # measurement against a configured SLO, not a pass. The
            # old `r.ttft_ms is not None and ...` form skipped the
            # check entirely when ttft_ms was None, silently counting
            # an unmeasured request as SLO-compliant.
            if ttft_slo_ms is not None and (r.ttft_ms is None or r.ttft_ms > ttft_slo_ms):
                return False
            return True

        good = [r for r in results if meets_slo(r)]
        slo_goodput_rps = round(len(good) / duration_s, 4)
        reference_rps = offered_rps if offered_rps is not None else round(n / duration_s, 4)
        slo_efficiency = round(slo_goodput_rps / reference_rps, 4) if reference_rps > 0 else None

    return RunMetrics(
        n=n,
        success_rate=round(len(successes) / n, 4),
        throttle_rate=round(sum(1 for r in results if r.throttled) / n, 4),
        timeout_rate=round(sum(1 for r in results if r.timed_out) / n, 4),
        request_throughput_rps=round(len(successes) / duration_s, 4),
        token_throughput_tps=round(sum(output_tokens) / duration_s, 2) if output_tokens else None,
        latency_p50_ms=round(percentile(latencies, 50), 2),
        latency_p95_ms=round(percentile(latencies, 95), 2),
        latency_p99_ms=round(percentile(latencies, 99), 2),
        ttft_p50_ms=round(percentile(ttfts, 50), 2) if ttfts else None,
        ttft_p95_ms=round(percentile(ttfts, 95), 2) if ttfts else None,
        ttft_p99_ms=round(percentile(ttfts, 99), 2) if ttfts else None,
        slo_goodput_rps=slo_goodput_rps,
        slo_efficiency=slo_efficiency,
    )
