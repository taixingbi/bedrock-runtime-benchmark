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
from typing import Dict, List, Optional, Sequence, Tuple

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


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def rate_upper(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> float:
    """One-sided EXACT (Clopper-Pearson) upper bound on a binomial rate:
    the largest p for which seeing <= k events in n requests still has
    probability >= 1 - confidence. "With this many samples, the true
    rate is at most X at this confidence" -- with guaranteed coverage.

    Exact rather than Wilson on purpose: the SLO rate checks live at
    zero or a handful of events, exactly where Wilson is anti-
    conservative -- its 95% bound clears a 0.1% limit after 2,703
    throttle-free requests, but at a true rate of exactly 0.1% that
    happens 6.7% of the time, not 5%. Clopper-Pearson needs 2,995."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    alpha = 1.0 - confidence
    if k == 0:
        return 1.0 - alpha ** (1.0 / n)
    # P(X <= k | n, p) = 1 - I_p(k + 1, n - k); solve it equal to alpha.
    lo, hi = k / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1.0 - _betainc(k + 1, n - k, mid) > alpha:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return hi


def rate_lower(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> float:
    """Exact one-sided lower bound on a binomial rate (k successes of n)."""
    if n <= 0:
        return 0.0
    return 1.0 - rate_upper(n - k, n, confidence=confidence)


def wilson_upper(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> float:
    """One-sided Wilson score upper bound. Kept for reference/comparison
    only -- SLO decisions use the exact rate_upper (see its docstring)."""
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
    """Smallest n for which ZERO observed events already puts the exact
    upper bound at or under max_rate: 1 - alpha^(1/n) <= max_rate. The
    floor below which a point can't statistically demonstrate it meets
    the SLO no matter how clean it looks -- 2,995 requests for 0.1% at
    95% (the "rule of three": ~3 / rate)."""
    if max_rate <= 0:
        raise ValueError("max_rate must be > 0 -- a zero-tolerance rate can never be statistically demonstrated")
    if max_rate >= 1:
        return 1
    return math.ceil(math.log(1.0 - confidence) / math.log1p(-max_rate))


def tpot_ms(r: RequestResult) -> Optional[float]:
    """Time per output token for one request: the decode time after the
    first token, spread over the remaining tokens. None unless it's a
    successful streamed request with >= 2 output tokens (TTFT unknown or
    nothing decoded after the first token -> no meaningful TPOT)."""
    if not r.success or r.ttft_ms is None or r.latency_ms is None or not r.output_tokens or r.output_tokens < 2:
        return None
    return (r.latency_ms - r.ttft_ms) / (r.output_tokens - 1)


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
    tpot_p50_ms: Optional[float] = None
    tpot_p95_ms: Optional[float] = None
    tpot_p99_ms: Optional[float] = None
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
    slo_by_workload: Optional[Dict[str, Tuple[Optional[float], ...]]] = None,
    tpot_slo_ms: Optional[float] = None,
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

    `slo_by_workload` ({workload: (ttft_slo_ms, latency_slo_ms[, tpot_slo_ms])}) judges
    each request against its own class's SLO -- for a mixed workload,
    where a long generation and a short reply have different latency
    budgets. Requests whose class isn't listed use the scalar SLOs.

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
    tpots = [t for t in (tpot_ms(r) for r in population) if t is not None]
    output_tokens = [r.output_tokens for r in completed if r.output_tokens is not None]

    slo_goodput_rps = None
    slo_efficiency = None
    default_slo = (ttft_slo_ms, latency_slo_ms, tpot_slo_ms)
    class_slo = {k: (tuple(v) + (None, None, None))[:3] for k, v in (slo_by_workload or {}).items()}
    has_class_slo = any(any(x is not None for x in v) for v in class_slo.values())
    if any(x is not None for x in default_slo) or has_class_slo:
        def meets_slo(r: RequestResult) -> bool:
            if not r.success:
                return False
            ttft_slo, latency_slo, tpot_slo = class_slo.get(r.tags.get("workload"), default_slo)
            # Same fail-closed rule as TTFT below: a configured TPOT SLO
            # with no TPOT measured (non-streaming, < 2 output tokens) is
            # a missing measurement, not a pass.
            if tpot_slo is not None:
                t = tpot_ms(r)
                if t is None or t > tpot_slo:
                    return False
            if latency_slo is not None and (r.latency_ms is None or r.latency_ms > latency_slo):
                return False
            # A TTFT SLO is configured but this result has no TTFT at
            # all (non-streaming request, or a streaming measurement
            # that failed to capture one) -- that's a missing/invalid
            # measurement against a configured SLO, not a pass. The
            # old `r.ttft_ms is not None and ...` form skipped the
            # check entirely when ttft_ms was None, silently counting
            # an unmeasured request as SLO-compliant.
            if ttft_slo is not None and (r.ttft_ms is None or r.ttft_ms > ttft_slo):
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
        tpot_p50_ms=round(percentile(tpots, 50), 3) if tpots else None,
        tpot_p95_ms=round(percentile(tpots, 95), 3) if tpots else None,
        tpot_p99_ms=round(percentile(tpots, 99), 3) if tpots else None,
        slo_goodput_rps=slo_goodput_rps,
        slo_efficiency=slo_efficiency,
        n_throttled=n_throttled,
        throttle_rate_upper=round(rate_upper(n_throttled, n, confidence=confidence), 6),
        success_rate_lower=round(rate_lower(n_success, n, confidence=confidence), 6),
        bound_confidence=confidence,
        measured_duration_s=round(duration_s, 3),
    )
