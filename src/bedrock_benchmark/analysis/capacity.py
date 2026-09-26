"""Recommendation engine, deliberately rule-based, not a "black box"
score: a config recommendation this repo hands to a gateway's own
control-plane config MUST be able to say exactly why it picked what it
picked. The pipeline is always:

    measurements -> SLO filtering -> pick max SLO-goodput among the
    survivors -> apply headroom -> recommended operating envelope

Never: fit a curve, guess a knee, or otherwise infer a number no single
measured point actually produced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .metrics import RunMetrics


@dataclass
class SweepPoint:
    """One measured point in a sweep -- exactly one of concurrency/rps
    is set, matching which runner produced it (ConcurrencyRunner vs
    RateRunner)."""
    concurrency: Optional[int]
    rps: Optional[float]
    # Pooled across every repetition's measurement window -- what the
    # SLO gate and the recommendation read.
    metrics: RunMetrics
    # One entry per repetition, kept so run-to-run spread is visible
    # instead of hidden inside the pooled number.
    repetitions: List[RunMetrics] = field(default_factory=list)
    # Mixed-workload sweeps only: pooled metrics per drawn class. The
    # SLO must hold for EVERY class, not just the blend -- a 70/30
    # short/long mix can have a fine aggregate p95 while the long
    # class alone blows its latency SLO.
    class_metrics: Dict[str, RunMetrics] = field(default_factory=dict)
    # Most calls ever outstanding in the client (running + waiting for
    # a thread) during this point, and whether that exceeded the thread
    # pool -- a client_limited point measured client-side queueing, not
    # Bedrock, and can never be recommended (fails closed).
    peak_outstanding: Optional[int] = None
    client_limited: bool = False


def meets_slo(
    metrics: RunMetrics, *,
    success_rate_min: float = 0.99, throttle_rate_max: float = 0.001,
    ttft_p95_slo_ms: Optional[float] = None, latency_p95_slo_ms: Optional[float] = None,
    gate_on_bounds: bool = False,
) -> bool:
    """gate_on_bounds=True gates the two rate SLOs on their confidence
    bounds (success_rate_lower / throttle_rate_upper) instead of the
    raw point estimates -- a point must DEMONSTRATE it meets the SLO
    at the measured sample size, not merely fail to observe a
    violation. Too few requests then fails closed, the same way a
    configured-but-unmeasured TTFT SLO does below."""
    if metrics.n == 0:
        return False
    success_rate = metrics.success_rate
    throttle_rate = metrics.throttle_rate
    if gate_on_bounds:
        if metrics.success_rate_lower is None or metrics.throttle_rate_upper is None:
            return False
        success_rate = metrics.success_rate_lower
        throttle_rate = metrics.throttle_rate_upper
    if success_rate < success_rate_min:
        return False
    if throttle_rate > throttle_rate_max:
        return False
    # A configured TTFT SLO with no TTFT measurement at all (e.g.
    # stream: false, or every streaming call failed before its first
    # token) is a missing/invalid measurement, not a pass -- same fix
    # as metrics.py's per-request meets_slo, and for the same reason:
    # the old form silently skipped the check when ttft_p95_ms was None.
    if ttft_p95_slo_ms is not None and (metrics.ttft_p95_ms is None or metrics.ttft_p95_ms > ttft_p95_slo_ms):
        return False
    if latency_p95_slo_ms is not None and metrics.latency_p95_ms > latency_p95_slo_ms:
        return False
    return True


def point_meets_slo(point: SweepPoint, class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> bool:
    """The blend (point.metrics) is judged with slo_kwargs; each class of
    a mixed point with its own entry in class_slo (falling back to
    slo_kwargs), so every class meets ITS SLO."""
    if point.client_limited:
        return False
    return meets_slo(point.metrics, **slo_kwargs) and all(
        meets_slo(m, **(class_slo or {}).get(name, slo_kwargs)) for name, m in point.class_metrics.items()
    )


@dataclass
class SweepAnalysis:
    """Where a sweep crosses from passing to failing -- honest about
    noise. Real Bedrock sweeps aren't always monotonic (PASS, FAIL,
    PASS, FAIL can be provider noise or sampling variance), and naming
    the first failure "saturation" while recommending a point above it
    would contradict itself. So:

    - not_reached: every point passed -- the sweep never found the edge.
    - resolved:    passes then fails, cleanly -- saturation = first fail.
    - unresolved:  a pass after a fail -- no saturation is claimed;
                   stable_pass_max (end of the leading run of passes),
                   unstable_region (values between that and the last
                   pass) and confirmed_fail_from (first of the trailing
                   run of fails) describe it instead.
    """
    status: str  # "not_reached" | "resolved" | "unresolved" | "no_pass"
    stable_pass_max: Optional[float] = None
    unstable_region: List[float] = field(default_factory=list)
    confirmed_fail_from: Optional[float] = None


def _key(p: SweepPoint) -> float:
    return p.concurrency if p.concurrency is not None else p.rps


def analyze_sweep(points: List[SweepPoint], class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> SweepAnalysis:
    ordered = sorted(points, key=_key)
    passed = [point_meets_slo(p, class_slo, **slo_kwargs) for p in ordered]
    if not any(passed):
        return SweepAnalysis(status="no_pass")
    lead = next((i for i, ok in enumerate(passed) if not ok), len(passed))  # leading passes = ordered[:lead]
    last_pass = max(i for i, ok in enumerate(passed) if ok)
    stable_pass_max = _key(ordered[lead - 1]) if lead > 0 else None
    confirmed = _key(ordered[last_pass + 1]) if last_pass + 1 < len(ordered) else None
    if lead == len(ordered):
        return SweepAnalysis(status="not_reached", stable_pass_max=stable_pass_max)
    if last_pass < lead:
        return SweepAnalysis(status="resolved", stable_pass_max=stable_pass_max, confirmed_fail_from=confirmed)
    return SweepAnalysis(
        status="unresolved", stable_pass_max=stable_pass_max,
        unstable_region=[_key(p) for p in ordered[lead:last_pass + 1]], confirmed_fail_from=confirmed,
    )


@dataclass
class Recommendation:
    point: SweepPoint
    # The sweep's saturation edge -- the first point that FAILED the
    # SLO, set only when the sweep is cleanly monotonic
    # (analysis.status == "resolved"). None when every point passed
    # (not_reached: re-run with higher values) or when measurements
    # were non-monotonic (unresolved: see analysis).
    saturation_point: Optional[SweepPoint]
    analysis: SweepAnalysis = field(default_factory=lambda: SweepAnalysis(status="resolved"))


def recommend(points: List[SweepPoint], class_slo: Optional[Dict[str, dict]] = None, **slo_kwargs) -> Optional[Recommendation]:
    """Among the LEADING run of passing points (everything up to the
    first failure), picks the highest slo_goodput_rps -- ties broken
    toward the LOWER concurrency/rps. Points that pass only after an
    earlier failure are never recommended: a pass above a failure is
    exactly the noise a conservative envelope must not bet on. Returns
    None if no leading point passes (including a sweep whose very first
    point failed) -- see analyze_sweep for the why."""
    analysis = analyze_sweep(points, class_slo, **slo_kwargs)
    if analysis.stable_pass_max is None:
        return None
    ordered = sorted(points, key=_key)
    stable = [p for p in ordered if _key(p) <= analysis.stable_pass_max]
    best = max(stable, key=lambda p: (p.metrics.slo_goodput_rps or 0.0, -(_key(p) or 0)))
    saturation = None
    if analysis.status == "resolved":
        saturation = next(p for p in ordered if _key(p) == analysis.confirmed_fail_from)
    return Recommendation(point=best, saturation_point=saturation, analysis=analysis)


def apply_headroom(value: float, *, headroom: float) -> float:
    """A recommended concurrency/rps is a MEASURED ceiling, not a
    production target -- see this repo's own README on why running a
    tenant's normal traffic right up against a measured knee is exactly
    the mistake this tool exists to prevent. headroom=0.20 means "back
    off 20% from what was measured to still pass."""
    return round(value * (1.0 - headroom), 4)
