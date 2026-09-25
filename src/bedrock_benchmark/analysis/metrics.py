"""Turns a flat list of RequestResult into the numbers capacity.py's
recommendation engine actually reads. SLO goodput -- not raw
throughput -- is the headline number here (see this repo's own
README): a config that maximizes throughput while blowing through TTFT
SLO and drawing real throttling is a worse config than one with lower
raw throughput and zero SLO violations, and only slo_goodput_rps makes
that comparison possible at a glance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import List, Optional, Sequence

from ..results import RequestResult

DEFAULT_CONFIDENCE = 0.95


@dataclass(frozen=True)
class MeasurementWindow:
    """Wall-clock [start, end) interval a runner's load counts toward.
    A run is warmup -> measurement window -> drain: requests are fired
    across warmup + window, and whatever is still in flight when load
    stops is allowed to finish (drain) so its real latency/outcome is
    recorded -- but only the window itself is ever counted, see
    compute_run_metrics for exactly how."""
    start: float
    end: float

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


def _z_one_sided(confidence: float) -> float:
    return NormalDist().inv_cdf(confidence)


def wilson_upper(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> float:
    """One-sided Wilson score upper bound on a binomial rate -- "with
    this many samples, the true rate is at most X at this confidence."
    Used instead of the raw k/n point estimate because a 0.1% throttle
    SLO can't be resolved from a few hundred requests: 0/540 throttles
    has a point estimate of 0% but a 95% upper bound of ~0.5%."""
    if n <= 0:
        return 1.0
    z = _z_one_sided(confidence)
    p = k / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (center + margin) / denom)


def wilson_lower(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> float:
    if n <= 0:
        return 0.0
    return 1.0 - wilson_upper(n - k, n, confidence=confidence)


def min_samples_to_resolve_rate(max_rate: float, *, confidence: float = DEFAULT_CONFIDENCE) -> int:
    """Smallest n for which ZERO observed events already puts the
    Wilson upper bound at or under max_rate -- the floor below which a
    point can't statistically demonstrate it meets the SLO no matter
    how clean it looks. ~2,700 requests for 0.1% at 95%."""
    if max_rate <= 0:
        raise ValueError("max_rate must be > 0 -- a zero-tolerance rate can never be statistically demonstrated")
    z2 = _z_one_sided(confidence) ** 2
    return math.ceil(z2 * (1 - max_rate) / max_rate)


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
    # Raw counts + confidence bounds behind the two rate gates, so a
    # reader can tell "0% throttled out of 5,000" from "0% out of 50".
    n_throttled: int = 0
    throttle_rate_upper: Optional[float] = None
    success_rate_lower: Optional[float] = None
    bound_confidence: Optional[float] = None
    measured_duration_s: Optional[float] = None


def compute_run_metrics(
    results: List[RequestResult], *, duration_s: Optional[float] = None,
    windows: Optional[Sequence[MeasurementWindow]] = None,
    ttft_slo_ms: Optional[float] = None, latency_slo_ms: Optional[float] = None,
    offered_rps: Optional[float] = None, confidence: float = DEFAULT_CONFIDENCE,
) -> RunMetrics:
    """With `windows` (the normal path -- one per repetition), two
    different populations are used, each for the question it answers
    without bias:

    - rates + percentiles: every request SCHEDULED inside a window,
      whatever its outcome and however late it finished (drain). Using
      completions here instead would silently drop the slowest
      requests -- exactly the tail an SLO exists to catch.
    - throughput / goodput / token throughput: successes COMPLETED
      inside a window, divided by the window's own length. Counting
      the drain's completions here (the old behavior: every success /
      duration_s) overstates throughput by up to one full concurrency
      level's worth of requests per run.

    Without `windows`, falls back to "every result, over duration_s"
    (no warmup/drain distinction) -- kept for ad-hoc re-analysis of
    old JSONL that predates window tagging.
    """
    if windows:
        population = [r for r in results if any(w.contains(r.scheduled_at) for w in windows)]
        completed = [r for r in results if r.success and any(w.contains(r.completed_at) for w in windows)]
        duration_s = sum(w.duration_s for w in windows)
    else:
        population = list(results)
        completed = [r for r in results if r.success]
        duration_s = duration_s or 0.0

    n = len(population)
    if n == 0 or duration_s <= 0:
        return RunMetrics(
            n=0, success_rate=0.0, throttle_rate=0.0, timeout_rate=0.0,
            request_throughput_rps=0.0, token_throughput_tps=None,
            latency_p50_ms=0.0, latency_p95_ms=0.0, latency_p99_ms=0.0,
        )

    n_success = sum(1 for r in population if r.success)
    n_throttled = sum(1 for r in population if r.throttled)
    latencies = [r.latency_ms for r in population if r.latency_ms is not None]
    ttfts = [r.ttft_ms for r in population if r.ttft_ms is not None]
    output_tokens = [r.output_tokens for r in completed if r.output_tokens is not None]

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

        good = [r for r in completed if meets_slo(r)]
        slo_goodput_rps = round(len(good) / duration_s, 4)
        reference_rps = offered_rps if offered_rps is not None else round(n / duration_s, 4)
        slo_efficiency = round(slo_goodput_rps / reference_rps, 4) if reference_rps > 0 else None

    return RunMetrics(
        n=n,
        success_rate=round(n_success / n, 4),
        throttle_rate=round(n_throttled / n, 4),
        timeout_rate=round(sum(1 for r in population if r.timed_out) / n, 4),
        request_throughput_rps=round(len(completed) / duration_s, 4),
        token_throughput_tps=round(sum(output_tokens) / duration_s, 2) if output_tokens else None,
        latency_p50_ms=round(percentile(latencies, 50), 2),
        latency_p95_ms=round(percentile(latencies, 95), 2),
        latency_p99_ms=round(percentile(latencies, 99), 2),
        ttft_p50_ms=round(percentile(ttfts, 50), 2) if ttfts else None,
        ttft_p95_ms=round(percentile(ttfts, 95), 2) if ttfts else None,
        ttft_p99_ms=round(percentile(ttfts, 99), 2) if ttfts else None,
        slo_goodput_rps=slo_goodput_rps,
        slo_efficiency=slo_efficiency,
        n_throttled=n_throttled,
        throttle_rate_upper=round(wilson_upper(n_throttled, n, confidence=confidence), 6),
        success_rate_lower=round(wilson_lower(n_success, n, confidence=confidence), 6),
        bound_confidence=confidence,
        measured_duration_s=round(duration_s, 3),
    )
